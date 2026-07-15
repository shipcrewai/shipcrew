"""Tests for the GitHub webhook bridge and bridge payload parsing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest
from datetime import datetime
from httpx import ASGITransport, AsyncClient

from shipply.config import (
    DatabaseConfig,
    GateConfig,
    GitHubConfig,
    ObservabilityConfig,
    PersonaConfig,
    ShipplyConfig,
)
from shipply.handlers.gate3 import (
    handle_bridge_event,
    parse_bridge_payload,
    resolve_proposal_by_pr_url,
)
from shipply.webhook_bridge import (
    BRIDGE_SENDER_ID,
    BridgeSettings,
    EnvTokenProvider,
    InMemoryPactoPublisher,
    TokenProvider,
    UnixSocketPactoPublisher,
    WebhookBridge,
    _init_db,
    _record_delivery,
    _verify_signature,
    create_app,
)

WEBHOOK_SECRET = "test-webhook-secret"
PACTO_SECRET = "test-pacto-secret"
RECONCILE_TOKEN = "test-reconcile-token"


@pytest.fixture
def bridge_settings(tmp_path: Path) -> BridgeSettings:
    """Return bridge settings with an in-memory config."""
    config = ShipplyConfig(
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
            "gate-1": GateConfig(),
            "gate-2": GateConfig(),
            "gate-3": GateConfig(),
        },
        database=DatabaseConfig(path=str(tmp_path / "proposals.db")),
        observability=ObservabilityConfig(),
        github=GitHubConfig(
            source_org="source-org",
            workspace_org="workspace-org",
            repo_scope=["source-org/in-scope"],
            webhook_secret=WEBHOOK_SECRET,
            webhook_dedup_ttl_days=7,
        ),
    )
    return BridgeSettings(
        config=config,
        webhook_secret=WEBHOOK_SECRET,
        pacto_socket=str(tmp_path / "pacto.sock"),
        pacto_secret_token=PACTO_SECRET,
        reconcile_token=RECONCILE_TOKEN,
        db_path=str(tmp_path / "dedup.db"),
        max_body_size=1024,
    )


class FakeTokenProvider:
    """Token provider that returns a deterministic token."""

    def __init__(self, token: str = "gh-source-token") -> None:
        self.token = token

    async def get_source_token(self) -> str:
        return self.token


@pytest.fixture
async def bridge(bridge_settings: BridgeSettings) -> WebhookBridge:
    """Return a bridge with an in-memory DB and mock publisher."""
    publisher = InMemoryPactoPublisher()
    db = await aiosqlite.connect(bridge_settings.db_path)
    await _init_db(db)
    token_provider = FakeTokenProvider()
    instance = WebhookBridge(bridge_settings, publisher, token_provider, db=db)
    instance.run_reconcile_task = False
    yield instance
    await instance.close()


@pytest.fixture
async def client(bridge: WebhookBridge) -> AsyncClient:
    """Return an HTTP client for the test bridge app."""
    app = create_app(bridge)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Compute the GitHub HMAC-SHA256 signature for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_pr_payload(
    action: str = "opened",
    repo: str = "source-org/in-scope",
    number: int = 42,
    state: str = "open",
    updated_at: str = "2026-07-15T12:00:00Z",
) -> dict[str, Any]:
    """Build a minimal pull_request webhook payload."""
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "state": state,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "updated_at": updated_at,
        },
        "repository": {
            "full_name": repo,
        },
    }


# ---------------------------------------------------------------------------
# HMAC validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature,expected",
    [
        (lambda b: _sign(b), True),
        (lambda b: "sha256=" + "0" * 64, False),
        (lambda b: "sha1=abc123", False),
    ],
)
def test_verify_signature(signature, expected):
    body = b"hello webhook"
    sig = signature(body)
    assert _verify_signature(body, sig, WEBHOOK_SECRET) is expected


async def test_missing_signature_returns_401(client: AsyncClient):
    body = json.dumps(_make_pr_payload()).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
        },
    )
    assert response.status_code == 401


async def test_invalid_signature_returns_401(client: AsyncClient):
    body = json.dumps(_make_pr_payload()).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Happy path and deduplication
# ---------------------------------------------------------------------------


async def test_valid_pull_request_webhook_accepted(client: AsyncClient, bridge: WebhookBridge):
    payload = _make_pr_payload(action="opened", state="open")
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "delivery-valid-1"
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 202
    assert response.json()["delivery_id"] == delivery_id

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["shipply"] == "bridge_event"
    assert event["sender"] == BRIDGE_SENDER_ID
    assert event["delivery_id"] == delivery_id
    assert event["event_type"] == "pull_request"
    assert event["action"] == "opened"
    assert event["repo"] == "source-org/in-scope"
    assert event["pr_number"] == 42
    assert event["state"] == "open"


async def test_duplicate_delivery_ignored(client: AsyncClient, bridge: WebhookBridge):
    payload = _make_pr_payload()
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "delivery-dup-1"
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign(body),
    }

    response1 = await client.post("/webhook", content=body, headers=headers)
    assert response1.status_code == 202

    response2 = await client.post("/webhook", content=body, headers=headers)
    assert response2.status_code == 204

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 1


async def test_ttl_expiration_allows_reprocessing(client: AsyncClient, bridge: WebhookBridge):
    payload = _make_pr_payload()
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "delivery-ttl-1"
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign(body),
    }

    # First delivery is accepted.
    response1 = await client.post("/webhook", content=body, headers=headers)
    assert response1.status_code == 202

    # Manually expire the delivery row.
    expired = "2020-01-01T00:00:00+00:00"
    await bridge.db.execute(
        "UPDATE deliveries SET expires_at = ? WHERE delivery_id = ?",
        (expired, delivery_id),
    )
    await bridge.db.commit()

    # Re-delivery with the same ID should be processed again.
    response2 = await client.post("/webhook", content=body, headers=headers)
    assert response2.status_code == 202

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 2

    # Verify the TTL was refreshed.
    cursor = await bridge.db.execute(
        "SELECT expires_at FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    expires_at = datetime.fromisoformat(row[0])
    assert expires_at.year > 2020


# ---------------------------------------------------------------------------
# Out-of-scope and size limits
# ---------------------------------------------------------------------------


async def test_out_of_scope_repo_dropped(client: AsyncClient, bridge: WebhookBridge):
    payload = _make_pr_payload(repo="source-org/out-of-scope")
    body = json.dumps(payload).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-oos-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 204

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 0


async def test_oversized_body_returns_413(client: AsyncClient, bridge: WebhookBridge):
    # The test bridge uses a 1KB max body size for speed.
    big_body = b'{"x": "' + b"a" * bridge.settings.max_body_size + b'"}'
    response = await client.post(
        "/webhook",
        content=big_body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-big-1",
            "X-Hub-Signature-256": _sign(big_body),
        },
    )
    assert response.status_code == 413


async def test_content_length_too_large_returns_413(client: AsyncClient, bridge: WebhookBridge):
    body = b'{"action":"opened"}'
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-cl-1",
            "X-Hub-Signature-256": _sign(body),
            "Content-Length": str(bridge.settings.max_body_size + 1),
        },
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Allow-listing and review events
# ---------------------------------------------------------------------------


async def test_disallowed_event_type_dropped(client: AsyncClient, bridge: WebhookBridge):
    payload = {"action": "created", "repository": {"full_name": "source-org/in-scope"}}
    body = json.dumps(payload).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-issues-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 204


async def test_disallowed_action_dropped(client: AsyncClient, bridge: WebhookBridge):
    payload = _make_pr_payload(action="assigned")
    body = json.dumps(payload).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-assigned-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 204


async def test_pull_request_review_submitted_accepted(client: AsyncClient, bridge: WebhookBridge):
    payload = {
        "action": "submitted",
        "review": {
            "state": "approved",
            "submitted_at": "2026-07-15T13:00:00Z",
        },
        "pull_request": {
            "number": 42,
            "state": "open",
            "html_url": "https://github.com/source-org/in-scope/pull/42",
            "updated_at": "2026-07-15T13:00:00Z",
        },
        "repository": {"full_name": "source-org/in-scope"},
    }
    body = json.dumps(payload).encode("utf-8")
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request_review",
            "X-GitHub-Delivery": "delivery-review-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 202

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["event_type"] == "pull_request_review"
    assert event["action"] == "submitted"
    assert event["review_decision"] == "approved"


# ---------------------------------------------------------------------------
# Reconciliation endpoint
# ---------------------------------------------------------------------------


class MockAsyncClient:
    """Minimal async httpx client mock for the reconciliation tests."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url: str, **kwargs: Any):
        self.calls.append((url, kwargs))
        key = url.split("?")[0]
        data = self.responses.get(key, [])
        status = 200 if key in self.responses else 404

        class Response:
            status_code = status
            text = json.dumps(data)

            def json(self):
                return data

        return Response()


def _patch_httpx_client(monkeypatch, responses: dict[str, Any]) -> None:
    def factory(*args, **kwargs):
        return MockAsyncClient(responses)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_reconcile_endpoint_emits_events(
    client: AsyncClient, bridge: WebhookBridge, monkeypatch
):
    prs = [
        {
            "number": 10,
            "state": "open",
            "html_url": "https://github.com/source-org/in-scope/pull/10",
            "updated_at": "2026-07-15T14:00:00Z",
        }
    ]
    _patch_httpx_client(monkeypatch, {"/repos/source-org/in-scope/pulls": prs})

    response = await client.post(
        "/reconcile",
        headers={"X-Reconcile-Token": RECONCILE_TOKEN},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["checked"] == 1
    assert data["emitted"] == 1

    publisher = bridge.publisher
    assert isinstance(publisher, InMemoryPactoPublisher)
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["event_type"] == "pull_request"
    assert event["action"] == "reconciled"
    assert event["pr_number"] == 10


async def test_reconcile_endpoint_requires_token(client: AsyncClient, bridge: WebhookBridge):
    response = await client.post("/reconcile")
    assert response.status_code == 401

    response = await client.post(
        "/reconcile",
        headers={"X-Reconcile-Token": "wrong-token"},
    )
    assert response.status_code == 401


async def test_reconcile_does_not_emit_unchanged_state(
    client: AsyncClient, bridge: WebhookBridge, monkeypatch
):
    prs = [
        {
            "number": 10,
            "state": "open",
            "html_url": "https://github.com/source-org/in-scope/pull/10",
            "updated_at": "2026-07-15T14:00:00Z",
        }
    ]
    _patch_httpx_client(monkeypatch, {"/repos/source-org/in-scope/pulls": prs})

    response1 = await client.post(
        "/reconcile",
        headers={"X-Reconcile-Token": RECONCILE_TOKEN},
    )
    assert response1.json()["emitted"] == 1

    response2 = await client.post(
        "/reconcile",
        headers={"X-Reconcile-Token": RECONCILE_TOKEN},
    )
    assert response2.json()["emitted"] == 0


# ---------------------------------------------------------------------------
# Gate 3 bridge payload parsing
# ---------------------------------------------------------------------------


async def test_parse_bridge_payload_and_resolve_pr_url(seeded_db: Path):
    # seeded_db fixture has a proposal with molecules table containing pr_url.
    # We need to insert a molecule row with a PR URL for a known proposal.
    db = await aiosqlite.connect(seeded_db)
    try:
        await db.execute(
            "INSERT INTO molecules (proposal_id, molecule_id, pr_url) VALUES (?, ?, ?)",
            ("prop-001", "mol-webhook-1", "https://github.com/source-org/repo/pull/99"),
        )
        await db.commit()

        payload = {
            "shipply": "bridge_event",
            "delivery_id": "d-1",
            "sender": BRIDGE_SENDER_ID,
            "event_type": "pull_request",
            "action": "synchronize",
            "repo": "source-org/repo",
            "pr_number": 99,
            "pr_url": "https://github.com/source-org/repo/pull/99",
            "state": "open",
            "review_decision": None,
            "updated_at": "2026-07-15T15:00:00Z",
        }
        parsed = parse_bridge_payload(payload)
        assert parsed is not None
        assert parsed["pr_url"] == "https://github.com/source-org/repo/pull/99"
        assert parsed["action"] == "synchronize"

        resolved = await resolve_proposal_by_pr_url(db, parsed["pr_url"])
        assert resolved is not None
        assert resolved["proposal_id"] == "prop-001"
        assert resolved["proposal"]["state"] == "GATE_1"
    finally:
        await db.close()


async def test_parse_bridge_payload_rejects_invalid_pr_url():
    payload = {
        "shipply": "bridge_event",
        "pr_url": "https://example.com/not-a-pr",
    }
    assert parse_bridge_payload(payload) is None


async def test_handle_bridge_event_logs(seeded_db: Path):
    from shipply.db import get_db, init_db
    from shipply.observability import EventEmitter

    db = await get_db(seeded_db)
    await init_db(db)
    try:
        await db.execute(
            "INSERT INTO molecules (proposal_id, molecule_id, pr_url) VALUES (?, ?, ?)",
            ("prop-001", "mol-webhook-2", "https://github.com/source-org/repo/pull/88"),
        )
        await db.commit()

        payload = {
            "shipply": "bridge_event",
            "delivery_id": "d-2",
            "sender": BRIDGE_SENDER_ID,
            "event_type": "pull_request",
            "action": "closed",
            "repo": "source-org/repo",
            "pr_number": 88,
            "pr_url": "https://github.com/source-org/repo/pull/88",
            "state": "closed",
            "review_decision": None,
            "updated_at": "2026-07-15T15:00:00Z",
        }
        emitter = EventEmitter(db)
        result = await handle_bridge_event(db, emitter, payload)
        assert result is not None
        assert result["proposal_id"] == "prop-001"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Token provider and publisher unit tests
# ---------------------------------------------------------------------------


async def test_env_token_provider_reads_file(tmp_path: Path, monkeypatch):
    token_path = tmp_path / "source-token.txt"
    token_path.write_text("file-token\n")
    monkeypatch.setenv("GITHUB_TOKEN_SOURCE_FILE", str(token_path))
    monkeypatch.delenv("GITHUB_TOKEN_SOURCE", raising=False)

    provider = EnvTokenProvider()
    assert await provider.get_source_token() == "file-token"


async def test_unix_socket_publisher_raises_on_missing_socket(tmp_path: Path):
    publisher = UnixSocketPactoPublisher(str(tmp_path / "nonexistent.sock"), PACTO_SECRET)
    with pytest.raises(Exception):
        await publisher.publish({"test": "event"})
