"""GitHub webhook validation and Nostr bridge.

The bridge receives signed GitHub webhook payloads, validates their HMAC
signatures, deduplicates by ``X-GitHub-Delivery``, and publishes a sanitized
Nostr event to the local Pacto daemon over a Unix socket.  A reconciliation
loop also polls GitHub for open PR state changes that may have been missed.
"""

from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from shipply.config import GitHubConfig, ShipplyConfig, load_config

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover (python < 3.11)
    import tomli as tomllib

logger = logging.getLogger(__name__)

BRIDGE_SENDER_ID = "shipply-webhook-bridge"
MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB

ALLOWED_EVENT_ACTIONS = {
    "pull_request": {"opened", "synchronize", "closed", "reopened"},
    "pull_request_review": {"submitted"},
}


class WebhookBridgeError(Exception):
    """Base exception for webhook bridge errors."""


class PactoPublishError(WebhookBridgeError):
    """Raised when publishing to the Pacto daemon fails."""


class TokenProviderError(WebhookBridgeError):
    """Raised when a source token cannot be obtained."""


class ConfigurationError(WebhookBridgeError):
    """Raised when required bridge configuration is missing."""


class PactoPublisher(Protocol):
    """Abstract publisher for Nostr events to the Pacto daemon."""

    async def publish(self, event: dict[str, Any]) -> None:
        """Publish ``event`` to the Pacto daemon."""
        ...


class InMemoryPactoPublisher:
    """Publisher that records events in memory for testing."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()


class UnixSocketPactoPublisher:
    """Publish JSON events to the Pacto daemon over a Unix socket."""

    def __init__(self, socket_path: str, secret_token: str) -> None:
        self.socket_path = socket_path
        self.secret_token = secret_token

    async def publish(self, event: dict[str, Any]) -> None:
        envelope = {
            "token": self.secret_token,
            "sender": BRIDGE_SENDER_ID,
            "payload": event,
        }
        payload = json.dumps(envelope).encode("utf-8")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise PactoPublishError(f"failed to connect to Pacto socket: {exc}") from exc

        try:
            writer.write(payload + b"\n")
            await writer.drain()
            # Read a simple acknowledgment if the daemon sends one.
            try:
                ack = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2.0)
                if ack.strip() != b"OK":
                    logger.warning("unexpected Pacto ack: %r", ack)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
        except OSError as exc:
            raise PactoPublishError(f"failed to write to Pacto socket: {exc}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class TokenProvider(Protocol):
    """Abstract source-org token provider."""

    async def get_source_token(self) -> str:
        """Return a valid GitHub installation token for the source org."""
        ...


class EnvTokenProvider:
    """Read a source token from an environment variable or file."""

    def __init__(self, env_var: str = "GITHUB_TOKEN_SOURCE", file_env_var: str = "GITHUB_TOKEN_SOURCE_FILE") -> None:
        self.env_var = env_var
        self.file_env_var = file_env_var

    async def get_source_token(self) -> str:
        token = os.environ.get(self.env_var)
        if not token:
            path = os.environ.get(self.file_env_var)
            if path:
                token = Path(path).read_text().strip()
        if not token:
            raise TokenProviderError(f"{self.env_var} is not set and no file was provided")
        return token


class HttpTokenManagerClient:
    """Request a source-org token from a token-manager HTTP endpoint."""

    def __init__(self, base_url: str, secret_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret_token = secret_token

    async def get_source_token(self) -> str:
        import httpx

        headers: dict[str, str] = {}
        if self.secret_token:
            headers["Authorization"] = f"Bearer {self.secret_token}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/token/source", headers=headers)
        except httpx.HTTPError as exc:
            raise TokenProviderError(f"token manager request failed: {exc}") from exc
        if response.status_code != 200:
            raise TokenProviderError(
                f"token manager returned {response.status_code}: {response.text}"
            )
        data = response.json()
        token = data.get("token")
        if not token:
            raise TokenProviderError("token manager response did not contain a token")
        return token


@dataclass
class BridgeSettings:
    """Runtime settings derived from environment and config files."""

    config: ShipplyConfig
    webhook_secret: str
    pacto_socket: str
    pacto_secret_token: str
    reconcile_token: str
    db_path: str
    max_body_size: int = MAX_BODY_SIZE

    @property
    def github(self) -> GitHubConfig:
        return self.config.github


def _load_secret_from_env_or_file(
    env_var: str, file_env_var: str | None = None, default: str | None = None
) -> str | None:
    """Return a secret from ``env_var``, an optional file, or ``default``."""
    value = os.environ.get(env_var)
    if value:
        return value
    if file_env_var:
        path = os.environ.get(file_env_var)
        if path and Path(path).is_file():
            return Path(path).read_text().strip()
    return default


def _default_settings() -> BridgeSettings:
    """Build ``BridgeSettings`` from environment variables and ``shipply.toml``."""
    config_path = os.environ.get("SHIPPLY_CONFIG", "shipply.toml")
    config = load_config(config_path)

    webhook_secret = _load_secret_from_env_or_file(
        "GITHUB_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET_FILE"
    )
    if not webhook_secret:
        if config.github.webhook_secret:
            webhook_secret = config.github.webhook_secret
        else:
            raise ConfigurationError("GitHub webhook secret is not configured")

    pacto_secret_token = _load_secret_from_env_or_file(
        "PACTO_SECRET_TOKEN", "PACTO_SECRET_TOKEN_FILE"
    ) or ""

    reconcile_token = _load_secret_from_env_or_file(
        "RECONCILE_TOKEN", "RECONCILE_TOKEN_FILE"
    )
    if not reconcile_token:
        raise ConfigurationError("RECONCILE_TOKEN is not configured")

    return BridgeSettings(
        config=config,
        webhook_secret=webhook_secret,
        pacto_socket=os.environ.get("PACTO_SOCKET", "/run/pacto/pacto-bot-api.sock"),
        pacto_secret_token=pacto_secret_token,
        reconcile_token=reconcile_token,
        db_path=os.environ.get("BRIDGE_DB_PATH", "/var/lib/shipply-bridge/dedup.db"),
    )


async def _init_db(db: aiosqlite.Connection) -> None:
    """Create the deduplication and reconciliation tables."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            action TEXT,
            repo TEXT NOT NULL,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_expires_at ON deliveries(expires_at)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS pr_state (
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            state TEXT NOT NULL,
            review_decision TEXT,
            updated_at TEXT,
            last_emitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (repo, pr_number)
        )
        """
    )
    await db.commit()


async def _is_duplicate(
    db: aiosqlite.Connection, delivery_id: str, ttl_days: int
) -> tuple[bool, bool]:
    """Return ``(already_processed, expired)`` for a delivery ID.

    A duplicate is ``expired`` when its TTL has passed, meaning the bridge
    should process the event again and refresh the TTL.
    """
    cursor = await db.execute(
        "SELECT expires_at FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return False, False

    expires_at = datetime.fromisoformat(row[0])
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > now:
        return True, False
    return True, True


async def _record_delivery(
    db: aiosqlite.Connection,
    delivery_id: str,
    event_type: str,
    action: str | None,
    repo: str,
    ttl_days: int,
) -> None:
    """Persist a delivery ID with a refreshed TTL."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    await db.execute(
        """
        INSERT INTO deliveries (delivery_id, event_type, action, repo, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(delivery_id) DO UPDATE SET
            event_type=excluded.event_type,
            action=excluded.action,
            repo=excluded.repo,
            expires_at=excluded.expires_at
        """,
        (delivery_id, event_type, action, repo, expires_at.isoformat()),
    )
    await db.commit()


async def _cleanup_expired(db: aiosqlite.Connection) -> None:
    """Remove delivery rows whose TTL has expired."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("DELETE FROM deliveries WHERE expires_at <= ?", (now,))
    await db.commit()


async def _get_db(path: str) -> aiosqlite.Connection:
    """Open and initialize the SQLite database."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    await _init_db(db)
    return db


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification for ``X-Hub-Signature-256``."""
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _repo_in_scope(repo_full_name: str, scope: list[str]) -> bool:
    """Return True if ``repo_full_name`` is covered by the configured scope.

    An empty scope list allows every repository (useful for development);
    a non-empty list requires an exact match.
    """
    if not scope:
        return True
    return repo_full_name in scope


def _build_bridge_payload(
    delivery_id: str,
    event_type: str,
    action: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Transform a GitHub webhook payload into a bridge Nostr event payload."""
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name") or "unknown/unknown"

    pr = payload.get("pull_request") or {}
    review = payload.get("review") or {}

    pr_number = pr.get("number")
    if pr_number is None:
        pr_number = payload.get("number")

    pr_url = pr.get("html_url")
    if not pr_url and "pull_request" in payload:
        # Fallback for review payloads where the PR object is nested.
        pr_url = pr.get("html_url")

    review_decision = review.get("state")
    if event_type == "pull_request_review":
        review_decision = review.get("state")

    return {
        "shipply": "bridge_event",
        "delivery_id": delivery_id,
        "sender": BRIDGE_SENDER_ID,
        "event_type": event_type,
        "action": action,
        "repo": repo_full_name,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "state": pr.get("state"),
        "review_decision": review_decision,
        "updated_at": pr.get("updated_at") or review.get("submitted_at"),
    }


class WebhookBridge:
    """Validate webhooks, deduplicate them, and publish bridge events."""

    def __init__(
        self,
        settings: BridgeSettings,
        publisher: PactoPublisher,
        token_provider: TokenProvider,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        self.settings = settings
        self.publisher = publisher
        self.token_provider = token_provider
        self._db = db
        self._reconcile_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.run_reconcile_task = True

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise WebhookBridgeError("database not initialized")
        return self._db

    async def init_db(self) -> None:
        """Open the database if it was not provided at construction."""
        if self._db is None:
            self._db = await _get_db(self.settings.db_path)

    async def close(self) -> None:
        """Close the database connection and stop the reconcile loop."""
        self._stop_event.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def verify_signature(self, body: bytes, signature: str | None) -> None:
        """Validate the HMAC signature, raising 401 if it is missing or invalid."""
        if not signature:
            raise HTTPException(status_code=401, detail="missing signature")
        if not _verify_signature(body, signature, self.settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid signature")

    async def handle_webhook(
        self,
        body: bytes,
        signature: str | None,
        event_type: str | None,
        delivery_id: str | None,
    ) -> Response:
        """Process a GitHub webhook payload and publish a Nostr event."""
        # HMAC validation happens before any other processing.
        await self.verify_signature(body, signature)

        if not delivery_id:
            raise HTTPException(status_code=400, detail="missing delivery id")

        if event_type not in ALLOWED_EVENT_ACTIONS:
            return Response(status_code=204)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a json object")

        action = payload.get("action")
        if action not in ALLOWED_EVENT_ACTIONS[event_type]:
            return Response(status_code=204)

        repository = payload.get("repository") or {}
        repo_full_name = repository.get("full_name")
        if not repo_full_name:
            raise HTTPException(status_code=400, detail="missing repository full_name")

        if not _repo_in_scope(repo_full_name, self.settings.github.repo_scope):
            logger.info("dropping out-of-scope repo: %s", repo_full_name)
            return Response(status_code=204)

        ttl_days = self.settings.github.webhook_dedup_ttl_days
        is_dup, is_expired = await _is_duplicate(self.db, delivery_id, ttl_days)
        if is_dup and not is_expired:
            logger.info("duplicate delivery ignored: %s", delivery_id)
            return Response(status_code=204)

        bridge_payload = _build_bridge_payload(delivery_id, event_type, action, payload)
        await self.publisher.publish(bridge_payload)
        await _record_delivery(self.db, delivery_id, event_type, action, repo_full_name, ttl_days)
        await _cleanup_expired(self.db)

        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "delivery_id": delivery_id},
        )

    async def reconcile(self, repos: list[str] | None = None) -> dict[str, Any]:
        """Query GitHub for open PRs and emit Nostr events for missed changes."""
        import httpx

        token = await self.token_provider.get_source_token()
        scope = repos or self.settings.github.repo_scope or []
        if not scope:
            return {"checked": 0, "emitted": 0, "skipped_repos": 0}

        emitted = 0
        checked = 0
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(base_url="https://api.github.com", timeout=30.0) as client:
            for repo in scope:
                prs = await self._list_open_prs(client, headers, repo)
                for pr in prs:
                    checked += 1
                    if await self._maybe_emit_pr_state(repo, pr):
                        emitted += 1

        return {"checked": checked, "emitted": emitted, "skipped_repos": 0}

    async def _list_open_prs(
        self, client: Any, headers: dict[str, str], repo: str
    ) -> list[dict[str, Any]]:
        """Return open PRs for ``repo`` from the GitHub API."""
        import httpx

        try:
            response = await client.get(f"/repos/{repo}/pulls", headers=headers, params={"state": "open", "per_page": 100})
        except httpx.HTTPError as exc:
            logger.warning("failed to list open PRs for %s: %s", repo, exc)
            return []
        if response.status_code != 200:
            logger.warning(
                "GitHub returned %s for %s pulls: %s",
                response.status_code,
                repo,
                response.text,
            )
            return []
        return response.json()

    async def _maybe_emit_pr_state(self, repo: str, pr: dict[str, Any]) -> bool:
        """Emit a bridge event if the PR state has changed since the last run."""
        pr_number = pr.get("number")
        state = pr.get("state")
        updated_at = pr.get("updated_at")
        if pr_number is None or state is None:
            return False

        cursor = await self.db.execute(
            "SELECT state, updated_at FROM pr_state WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is not None:
            stored_state, stored_updated_at = row
            if stored_state == state and stored_updated_at == updated_at:
                return False

        bridge_payload = {
            "shipply": "bridge_event",
            "delivery_id": f"reconcile:{repo}:{pr_number}:{updated_at}",
            "sender": BRIDGE_SENDER_ID,
            "event_type": "pull_request",
            "action": "reconciled",
            "repo": repo,
            "pr_number": pr_number,
            "pr_url": pr.get("html_url"),
            "state": state,
            "review_decision": None,
            "updated_at": updated_at,
        }
        await self.publisher.publish(bridge_payload)
        await self.db.execute(
            """
            INSERT INTO pr_state (repo, pr_number, state, review_decision, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                state=excluded.state,
                review_decision=excluded.review_decision,
                updated_at=excluded.updated_at,
                last_emitted_at=CURRENT_TIMESTAMP
            """,
            (repo, pr_number, state, None, updated_at),
        )
        await self.db.commit()
        return True

    async def _reconcile_loop(self, interval_seconds: int = 300) -> None:
        """Run periodic reconciliation every ``interval_seconds``."""
        while not self._stop_event.is_set():
            try:
                await self.reconcile()
            except Exception:
                logger.exception("periodic reconciliation failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    def start_reconcile_task(self, interval_seconds: int = 300) -> None:
        """Start the background reconciliation task."""
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(interval_seconds)
            )


def _create_default_bridge() -> WebhookBridge:
    """Construct a bridge using environment-variable configuration."""
    settings = _default_settings()
    publisher = UnixSocketPactoPublisher(
        settings.pacto_socket, settings.pacto_secret_token
    )

    token_manager_url = os.environ.get("GITHUB_TOKEN_MANAGER_URL")
    if token_manager_url:
        token_provider: TokenProvider = HttpTokenManagerClient(
            token_manager_url,
            _load_secret_from_env_or_file("GITHUB_TOKEN_MANAGER_SECRET"),
        )
    else:
        token_provider = EnvTokenProvider()

    return WebhookBridge(settings, publisher, token_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the reconcile loop on startup and close on shutdown."""
    bridge: WebhookBridge = app.state.bridge
    await bridge.init_db()
    if bridge.run_reconcile_task:
        bridge.start_reconcile_task()
    yield
    await bridge.close()


def create_app(bridge: WebhookBridge | None = None) -> FastAPI:
    """Create the FastAPI/Starlette ASGI application."""
    app = FastAPI(title="Shipply Webhook Bridge", lifespan=lifespan)
    app.state.bridge = bridge or _create_default_bridge()

    @app.middleware("http")
    async def body_size_limit(request: Request, call_next):
        """Reject requests larger than the configured body size limit."""
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > app.state.bridge.settings.max_body_size:
                return Response(status_code=413)
        return await call_next(request)

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    ) -> Response:
        body = await request.body()
        if len(body) > app.state.bridge.settings.max_body_size:
            return Response(status_code=413)
        return await app.state.bridge.handle_webhook(
            body, x_hub_signature_256, x_github_event, x_github_delivery
        )

    @app.post("/reconcile")
    async def reconcile(
        request: Request,
        x_reconcile_token: str | None = Header(default=None, alias="X-Reconcile-Token"),
    ) -> JSONResponse:
        if x_reconcile_token is None:
            raise HTTPException(status_code=401, detail="invalid reconcile token")
        if not hmac.compare_digest(x_reconcile_token, app.state.bridge.settings.reconcile_token):
            raise HTTPException(status_code=401, detail="invalid reconcile token")
        body = await request.body()
        repos: list[str] | None = None
        if body:
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    repos = data.get("repos")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPException(status_code=400, detail="invalid json body")
        result = await app.state.bridge.reconcile(repos=repos)
        return JSONResponse(status_code=200, content=result)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(status_code=200, content={"status": "ok"})

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
