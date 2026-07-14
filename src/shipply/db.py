"""Async SQLite connection management and migration runner."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import aiosqlite

MIGRATIONS_DIR = Path(__file__).with_suffix("").parent / "migrations"


async def get_db(path: str | Path) -> aiosqlite.Connection:
    """Open an ``aiosqlite`` connection with WAL mode and a busy timeout.

    WAL mode lets readers and writers proceed concurrently across handler
    processes; the busy timeout causes writers to wait briefly rather than
    fail with ``SQLITE_BUSY`` under light contention.
    """
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    """Apply all raw SQL migrations in ``src/shipply/migrations`` in order.

    Migrations are idempotent (``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX
    IF NOT EXISTS`` / ``DROP VIEW IF EXISTS``) so ``init_db`` is safe to run on
    every handler startup.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for migration in migration_files:
        sql = migration.read_text(encoding="utf-8")
        await conn.executescript(sql)
    await conn.commit()


async def close_db(conn: aiosqlite.Connection) -> None:
    """Close the database connection."""
    await conn.close()


def migration_path() -> Path:
    """Return the directory containing SQL migration files."""
    return cast(Path, MIGRATIONS_DIR)
