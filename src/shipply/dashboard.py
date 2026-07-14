"""Lightweight async HTTP dashboard for Shipply observability."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp.web
import aiosqlite
from aiohttp.web import Request, Response
from prometheus_client import generate_latest

from shipply.config import load_config
from shipply.db import get_db, init_db
from shipply.observability import REGISTRY


async def health(_request: Request) -> Response:
    """Readiness check."""
    return aiohttp.web.json_response({"status": "ok"})


async def metrics(_request: Request) -> Response:
    """Prometheus exposition format."""
    body = generate_latest(REGISTRY)
    return Response(body=body, content_type="text/plain")


async def _fetchall(
    db: aiosqlite.Connection, sql: str, params: tuple[Any, ...] | None = None
) -> list[Any]:
    cursor = await db.execute(sql, params or ())
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def list_proposals(request: Request) -> Response:
    """List active (non-CLOSED) proposals with stage, age, and last event time."""
    db: aiosqlite.Connection = request.app["db"]
    rows = await _fetchall(
        db,
        """
        SELECT
            p.id,
            p.state,
            p.title,
            p.sponsor,
            p.created_at,
            MAX(e.created_at) AS last_event_at
        FROM proposals p
        LEFT JOIN events e ON e.proposal_id = p.id
        WHERE p.state != 'CLOSED'
        GROUP BY p.id
        ORDER BY p.created_at DESC
        """,
    )
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    def _to_iso(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _parse_dt(value: Any) -> datetime:
        if value is None:
            return now_naive
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except ValueError:
            return now_naive

    proposals = []
    for row in rows:
        created_at = row[4]
        created_dt = _parse_dt(created_at)
        age_seconds = int((now_naive - created_dt).total_seconds())
        proposals.append(
            {
                "id": row[0],
                "stage": row[1],
                "title": row[2],
                "sponsor": row[3],
                "age_seconds": age_seconds,
                "created_at": _to_iso(created_at),
                "last_event_at": _to_iso(row[5]),
            }
        )
    return aiohttp.web.json_response({"proposals": proposals})


async def get_proposal(request: Request) -> Response:
    """Return full timeline for a single proposal."""
    db: aiosqlite.Connection = request.app["db"]
    proposal_id = request.match_info["proposal_id"]

    proposal_rows = await _fetchall(
        db,
        "SELECT id, state, title, sponsor, created_at, updated_at FROM proposals WHERE id = ?",
        (proposal_id,),
    )
    if not proposal_rows:
        raise aiohttp.web.HTTPNotFound(
            text=json.dumps({"error": "proposal not found"}),
            content_type="application/json",
        )

    revisions = await _fetchall(
        db,
        """
        SELECT id, rev_num, requirements_doc, blueprint_doc, frozen_at
        FROM revisions
        WHERE proposal_id = ?
        ORDER BY rev_num ASC
        """,
        (proposal_id,),
    )
    events = await _fetchall(
        db,
        """
        SELECT id, event_type, stage, payload, created_at
        FROM events
        WHERE proposal_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (proposal_id,),
    )
    gates = await _fetchall(
        db,
        """
        SELECT id, gate_type, status, votes, metadata
        FROM gates
        WHERE proposal_id = ?
        ORDER BY gate_type ASC
        """,
        (proposal_id,),
    )
    beads = await _fetchall(
        db,
        """
        SELECT id, bead_id, status, molecule_id
        FROM beads
        WHERE proposal_id = ?
        ORDER BY bead_id ASC
        """,
        (proposal_id,),
    )

    def _parse_json(text: str | None) -> Any:
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    return aiohttp.web.json_response(
        {
            "proposal": {
                "id": proposal_rows[0][0],
                "state": proposal_rows[0][1],
                "title": proposal_rows[0][2],
                "sponsor": proposal_rows[0][3],
                "created_at": proposal_rows[0][4],
                "updated_at": proposal_rows[0][5],
            },
            "revisions": [
                {
                    "id": r[0],
                    "rev_num": r[1],
                    "requirements_doc": r[2],
                    "blueprint_doc": r[3],
                    "frozen_at": r[4],
                }
                for r in revisions
            ],
            "events": [
                {
                    "id": e[0],
                    "event_type": e[1],
                    "stage": e[2],
                    "payload": _parse_json(e[3]),
                    "created_at": e[4],
                }
                for e in events
            ],
            "gates": [
                {
                    "id": g[0],
                    "gate_type": g[1],
                    "status": g[2],
                    "votes": _parse_json(g[3]),
                    "metadata": _parse_json(g[4]),
                }
                for g in gates
            ],
            "beads": [
                {
                    "id": b[0],
                    "bead_id": b[1],
                    "status": b[2],
                    "molecule_id": b[3],
                }
                for b in beads
            ],
        }
    )


async def on_startup(app: aiohttp.web.Application) -> None:
    """Open the SQLite database and ensure schema is present."""
    config = load_config()
    db_path = Path(config.database.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await get_db(db_path)
    await init_db(db)
    app["db"] = db


async def on_cleanup(app: aiohttp.web.Application) -> None:
    """Close the SQLite connection."""
    db = app.get("db")
    if db is not None:
        await db.close()


def build_app() -> aiohttp.web.Application:
    """Construct the aiohttp application with routes and lifecycle hooks."""
    app = aiohttp.web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/proposals", list_proposals)
    app.router.add_get("/proposals/{proposal_id}", get_proposal)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def async_main(port: int | None = None) -> None:
    """Run the dashboard server."""
    config = load_config()
    app = build_app()
    listen_port = port if port is not None else config.observability.metrics_port
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", listen_port)
    await site.start()
    print(f"Shipply dashboard listening on http://0.0.0.0:{listen_port}")
    while True:
        await asyncio.sleep(3600)


def main() -> None:
    """Synchronous entrypoint for the dashboard console script."""
    parser = argparse.ArgumentParser(description="Shipply observability dashboard")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: metrics_port from config)",
    )
    args = parser.parse_args()
    asyncio.run(async_main(port=args.port))


if __name__ == "__main__":
    main()
