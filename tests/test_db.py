"""Tests for shipply database initialization and concurrency."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from shipply.db import close_db, get_db, init_db

pytestmark = pytest.mark.asyncio

EXPECTED_TABLES = {
    "proposals",
    "revisions",
    "beads",
    "gates",
    "events",
}

EXPECTED_VIEWS = {
    "v_proposals_in_flight",
    "v_beads_in_flight",
    "v_stage_latency",
}


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    """Yield an initialized SQLite connection backed by a temporary file."""
    path = tmp_path / "test.db"
    conn = await get_db(path)
    await init_db(conn)
    try:
        yield conn
    finally:
        await close_db(conn)


async def test_init_db_creates_tables_and_views(db: aiosqlite.Connection) -> None:
    """init_db() creates the expected tables and views."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    names = {name for (name,) in rows}
    assert EXPECTED_TABLES.issubset(names)
    assert EXPECTED_VIEWS.issubset(names)


async def test_init_db_is_idempotent(db: aiosqlite.Connection) -> None:
    """Running init_db() twice does not raise and keeps the schema intact."""
    await init_db(db)

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    names = {name for (name,) in rows}
    assert EXPECTED_TABLES.issubset(names)
    assert EXPECTED_VIEWS.issubset(names)


async def test_get_db_sets_wal_and_busy_timeout(tmp_path: Path) -> None:
    """get_db() enables WAL mode and sets a busy timeout."""
    path = tmp_path / "test.db"
    conn = await get_db(path)
    try:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0].upper() == "WAL"

        cursor = await conn.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] == 5000
    finally:
        await close_db(conn)


async def test_concurrent_writes_no_busy_errors(tmp_path: Path) -> None:
    """Concurrent proposal inserts do not raise SQLITE_BUSY under normal load."""
    path = tmp_path / "concurrent.db"

    async def insert_proposal(worker_id: int) -> int:
        conn = await get_db(path)
        try:
            await init_db(conn)
            cursor = await conn.execute(
                """
                INSERT INTO proposals (id, state, title, sponsor)
                VALUES (?, 'INTAKE', ?, 'tester')
                """,
                (f"p-{worker_id}", f"Proposal {worker_id}"),
            )
            await conn.commit()
            return cursor.rowcount
        finally:
            await close_db(conn)

    worker_count = 20
    results = await asyncio.gather(*(insert_proposal(i) for i in range(worker_count)))
    assert all(rowcount == 1 for rowcount in results)

    conn = await get_db(path)
    try:
        cursor = await conn.execute("SELECT COUNT(*) FROM proposals")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] == worker_count
    finally:
        await close_db(conn)


async def test_proposal_revision_bead_gate_schema_columns(db: aiosqlite.Connection) -> None:
    """Core tables contain the columns required by the models and migrations."""
    cursor = await db.execute("PRAGMA table_info(proposals)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = {r[1] for r in rows}
    assert columns >= {"id", "state", "title", "sponsor", "created_at", "updated_at"}

    cursor = await db.execute("PRAGMA table_info(revisions)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = {r[1] for r in rows}
    assert columns >= {"id", "proposal_id", "rev_num", "requirements_doc", "blueprint_doc", "frozen_at"}

    cursor = await db.execute("PRAGMA table_info(beads)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = {r[1] for r in rows}
    assert columns >= {"id", "proposal_id", "bead_id", "status", "molecule_id"}

    cursor = await db.execute("PRAGMA table_info(gates)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = {r[1] for r in rows}
    assert columns >= {"id", "proposal_id", "gate_type", "status", "votes", "metadata"}

    cursor = await db.execute("PRAGMA table_info(events)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = {r[1] for r in rows}
    assert columns >= {"id", "proposal_id", "event_type", "stage", "payload", "created_at"}
