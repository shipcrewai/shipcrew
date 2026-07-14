"""In-flight query CLI for Shipply observability."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import click

from shipply.config import load_config
from shipply.db import get_db, init_db


def _parse_json(text: str | None) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def _open_db() -> aiosqlite.Connection:
    config = load_config()
    db_path = Path(config.database.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await get_db(db_path)
    await init_db(db)
    return db


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """Shipply orchestration CLI."""


@cli.command()
def status() -> None:
    """Print a global in-flight summary."""
    import asyncio

    async def _run() -> None:
        db = await _open_db()
        try:
            # Proposals by stage
            cursor = await db.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM proposals
                WHERE state != 'CLOSED'
                GROUP BY state
                ORDER BY state
                """
            )
            stage_rows = await cursor.fetchall()
            await cursor.close()

            # Beads by status
            cursor = await db.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM beads
                GROUP BY status
                ORDER BY status
                """
            )
            bead_rows = await cursor.fetchall()
            await cursor.close()

            # Recent events
            cursor = await db.execute(
                """
                SELECT id, proposal_id, event_type, stage, created_at
                FROM events
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """
            )
            event_rows = await cursor.fetchall()
            await cursor.close()

            # p95 latency over last 24 hours: time spent in each stage per proposal
            cursor = await db.execute(
                """
                SELECT
                    stage,
                    ROUND(
                        (
                            MAX(
                                julianday(last_event_at) - julianday(entered_at)
                            ) * 24 * 3600
                        ),
                        2
                    ) AS p95_seconds
                FROM v_stage_latency
                WHERE last_event_at > datetime('now', '-1 day')
                GROUP BY stage
                ORDER BY stage
                """
            )
            latency_rows = await cursor.fetchall()
            await cursor.close()

            click.echo("=== Proposals in flight ===")
            if not stage_rows:
                click.echo("No active proposals.")
            else:
                for stage, count in stage_rows:
                    click.echo(f"{stage}: {count}")

            click.echo("\n=== Beads by status ===")
            if not bead_rows:
                click.echo("No beads.")
            else:
                for status, count in bead_rows:
                    click.echo(f"{status}: {count}")

            click.echo("\n=== p95 stage latency (24h) ===")
            if not latency_rows:
                click.echo("No stage latency data.")
            else:
                for stage, seconds in latency_rows:
                    click.echo(f"{stage}: {seconds}s")

            click.echo("\n=== Recent events ===")
            if not event_rows:
                click.echo("No events.")
            else:
                for event_id, proposal_id, event_type, stage, created_at in event_rows:
                    stage_part = f"[{stage}] " if stage else ""
                    click.echo(f"{created_at} {proposal_id} {stage_part}{event_type}")
        finally:
            await db.close()

    asyncio.run(_run())


@cli.command()
@click.argument("proposal_id")
@click.option("--follow", "-f", is_flag=True, help="Poll for new events (not implemented).")
def tail(proposal_id: str, follow: bool) -> None:
    """Print the event stream for a proposal, ordered by time."""
    import asyncio

    async def _run() -> None:
        db = await _open_db()
        try:
            cursor = await db.execute(
                """
                SELECT id, event_type, stage, payload, created_at
                FROM events
                WHERE proposal_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (proposal_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()

            if not rows:
                click.echo(f"No events for proposal {proposal_id}.")
                return

            for event_id, event_type, stage, payload, created_at in rows:
                stage_part = f"[{stage}] " if stage else ""
                payload_part = json.dumps(_parse_json(payload), sort_keys=True) if payload else "{}"
                click.echo(f"{created_at} {stage_part}{event_type} {payload_part}")

            if follow:
                click.echo("--follow is not yet implemented.")
        finally:
            await db.close()

    asyncio.run(_run())


def main() -> None:
    """Synchronous entrypoint for the console script."""
    cli()


if __name__ == "__main__":
    main()
