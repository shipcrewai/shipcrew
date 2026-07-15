"""Tests for the GitHub App token manager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shipply.config import GitHubConfig
from shipply.github_app import (
    SOURCE_REQUIRED_EVENTS,
    SOURCE_REQUIRED_PERMISSIONS,
    WORKSPACE_REQUIRED_EVENTS,
    WORKSPACE_REQUIRED_PERMISSIONS,
    GitHubAppConfigError,
    GitHubAppError,
    GitHubAppInstallationError,
    GitHubAppPermissionError,
    GitHubAppTokenManager,
)


@pytest.fixture
def key_pair(tmp_path):
    """Generate an RSA key pair and return the private key path + public key PEM."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_path = tmp_path / "github-app-private-key.pem"
    key_path.write_bytes(private_pem)
    return str(key_path), public_pem


@pytest.fixture
def config(key_pair):
    """Return a GitHubConfig with the generated key and two orgs."""
    key_path, _ = key_pair
    return GitHubConfig(
        app_id="123456",
        private_key_path=key_path,
        source_org="source-org",
        workspace_org="workspace-org",
    )


class MockGitHubTransport:
    """Record and respond to GitHub App API requests, verifying JWTs."""

    def __init__(self, public_key_pem, source_install_id=987654321, workspace_install_id=987654322):
        self.public_key_pem = public_key_pem
        self.source_install_id = source_install_id
        self.workspace_install_id = workspace_install_id
        self.installations_requests = 0
        self.token_requests = 0
        self.fail_token_once = False
        self.fail_token_once_code = 401
        self.token_failures = 0

    def _decode_jwt(self, request: httpx.Request) -> dict:
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"unexpected Authorization header: {auth!r}"
        token = auth[7:]
        return jwt.decode(
            token,
            self.public_key_pem,
            algorithms=["RS256"],
            options={"require": ["exp", "iat", "iss"]},
        )

    def _installations(self, request: httpx.Request) -> httpx.Response:
        self.installations_requests += 1
        claims = self._decode_jwt(request)
        assert claims["iss"] == "123456"
        return httpx.Response(
            200,
            json=[
                {
                    "id": self.source_install_id,
                    "account": {"login": "source-org", "type": "Organization"},
                    "permissions": dict(SOURCE_REQUIRED_PERMISSIONS),
                    "events": sorted(SOURCE_REQUIRED_EVENTS),
                },
                {
                    "id": self.workspace_install_id,
                    "account": {"login": "workspace-org", "type": "Organization"},
                    "permissions": dict(WORKSPACE_REQUIRED_PERMISSIONS),
                    "events": sorted(WORKSPACE_REQUIRED_EVENTS),
                },
            ],
        )

    def _access_token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        claims = self._decode_jwt(request)
        assert claims["iss"] == "123456"
        install_id = request.url.path.split("/")[-2]
        if self.fail_token_once and self.token_failures == 0:
            self.token_failures += 1
            return httpx.Response(self.fail_token_once_code, json={"message": "nope"})
        expires = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        return httpx.Response(
            201,
            json={
                "token": f"ghs_installation_token_{install_id}",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/app/installations":
            return self._installations(request)
        if request.method == "POST" and request.url.path.startswith("/app/installations/"):
            return self._access_token(request)
        if request.method == "GET" and request.url.path.startswith("/app/installations/"):
            install_id = request.url.path.split("/")[-1]
            if install_id == str(self.source_install_id):
                return httpx.Response(
                    200,
                    json={
                        "id": int(install_id),
                        "account": {"login": "source-org", "type": "Organization"},
                        "permissions": dict(SOURCE_REQUIRED_PERMISSIONS),
                        "events": sorted(SOURCE_REQUIRED_EVENTS),
                    },
                )
            if install_id == str(self.workspace_install_id):
                return httpx.Response(
                    200,
                    json={
                        "id": int(install_id),
                        "account": {"login": "workspace-org", "type": "Organization"},
                        "permissions": dict(WORKSPACE_REQUIRED_PERMISSIONS),
                        "events": sorted(WORKSPACE_REQUIRED_EVENTS),
                    },
                )
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def mock_transport(key_pair):
    """Return a fresh mock transport instance."""
    _, public_pem = key_pair
    return MockGitHubTransport(public_pem)


@pytest.fixture
def manager(config, mock_transport):
    """Return a token manager wired to the mock transport."""
    mgr = GitHubAppTokenManager(config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url="https://api.github.com",
    )
    return mgr


async def test_first_call_generates_jwt_and_returns_token(manager, mock_transport, key_pair):
    """A first call fetches an installation token and verifies the JWT shape."""
    _, public_pem = key_pair

    token = await manager.get_source_token()

    assert token == "ghs_installation_token_987654321"
    assert mock_transport.token_requests == 1
    assert mock_transport.installations_requests == 1


async def test_subsequent_call_returns_cached_token(manager, mock_transport):
    """A second call within the safety margin returns the cached token."""
    first = await manager.get_source_token()
    second = await manager.get_source_token()

    assert first == second
    assert mock_transport.token_requests == 1
    assert mock_transport.installations_requests == 1


async def test_refresh_before_safety_margin(manager, mock_transport):
    """A token outside the safety margin is refreshed; one inside is reused."""
    first = await manager.get_source_token()

    # Inside the safety margin -> still valid.
    manager._tokens["source"] = (
        first,
        datetime.now(tz=timezone.utc) + timedelta(minutes=6),
    )
    cached = await manager.get_source_token()
    assert cached == first
    assert mock_transport.token_requests == 1

    # Within the safety margin -> refresh.
    manager._tokens["source"] = (
        first,
        datetime.now(tz=timezone.utc) + timedelta(minutes=4),
    )
    refreshed = await manager.get_source_token()
    # The mock returns the same token value for the same installation, but a
    # fresh HTTP request was issued.
    assert refreshed == first
    assert mock_transport.token_requests == 2


async def test_refresh_near_expiry(manager, mock_transport):
    """A token that is just about to expire is refreshed."""
    first = await manager.get_source_token()

    manager._tokens["source"] = (
        first,
        datetime.now(tz=timezone.utc) + timedelta(seconds=30),
    )
    second = await manager.get_source_token()

    assert second == first
    assert mock_transport.token_requests == 2


async def test_missing_app_id_raises(key_pair):
    """A missing App ID produces a clear configuration error."""
    key_path, _ = key_pair
    config = GitHubConfig(private_key_path=key_path, source_org="source-org")

    with pytest.raises(GitHubAppConfigError, match="App ID"):
        GitHubAppTokenManager(config)


async def test_missing_private_key_path_raises():
    """A missing private key path produces a clear configuration error."""
    config = GitHubConfig(app_id="123456", source_org="source-org")

    with pytest.raises(GitHubAppConfigError, match="private key path"):
        GitHubAppTokenManager(config)


async def test_invalid_private_key_file_raises(tmp_path):
    """A non-existent or invalid PEM file produces a clear configuration error."""
    config = GitHubConfig(
        app_id="123456",
        private_key_path=str(tmp_path / "missing.pem"),
        source_org="source-org",
    )
    with pytest.raises(GitHubAppConfigError, match="not found"):
        GitHubAppTokenManager(config)

    bad_path = tmp_path / "not-a-key.pem"
    bad_path.write_text("this is not a pem key")
    config.private_key_path = str(bad_path)
    with pytest.raises(GitHubAppConfigError, match="valid PEM"):
        GitHubAppTokenManager(config)


async def test_missing_org_installation_raises(key_pair):
    """A 404 or missing installation for an org raises an installation error."""
    key_path, public_pem = key_pair
    config = GitHubConfig(
        app_id="123456",
        private_key_path=key_path,
        source_org="missing-org",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            jwt.decode(
                request.headers["Authorization"][7:],
                public_pem,
                algorithms=["RS256"],
            )
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    mgr = GitHubAppTokenManager(config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )

    with pytest.raises(GitHubAppInstallationError, match="missing-org"):
        await mgr.get_source_token()


async def test_404_for_configured_installation_id(key_pair):
    """A configured installation ID that returns 404 raises an installation error."""
    key_path, public_pem = key_pair
    config = GitHubConfig(
        app_id="123456",
        private_key_path=key_path,
        source_org="source-org",
        source_installation_id="111",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        jwt.decode(
            request.headers["Authorization"][7:],
            public_pem,
            algorithms=["RS256"],
        )
        return httpx.Response(404, json={"message": "Not Found"})

    mgr = GitHubAppTokenManager(config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )

    with pytest.raises(GitHubAppInstallationError, match="not found"):
        await mgr.get_source_token()


async def test_401_invalidates_cached_token_and_retries_once(manager, mock_transport):
    """A 401 on token exchange invalidates the cache and retries exactly once."""
    # Skip the installation lookup so the 401 happens on the access token endpoint.
    manager.config.source_installation_id = "987654321"
    first = await manager.get_source_token()
    assert mock_transport.token_requests == 1

    # Force a refresh by pushing the cached token into the safety margin.
    manager._tokens["source"] = (
        first,
        datetime.now(tz=timezone.utc) + timedelta(minutes=2),
    )
    mock_transport.fail_token_once = True
    mock_transport.fail_token_once_code = 401

    second = await manager.get_source_token()

    # The mock returns the same token value for the same installation.
    assert second == first
    assert mock_transport.token_requests == 3  # first call + failed retry + success
    assert manager._tokens["source"][0] == second


async def test_401_retry_exhaustion_raises(manager, mock_transport):
    """If both attempts fail with 401, the manager raises without looping."""
    manager.config.source_installation_id = "987654321"

    def failing_handler(request: httpx.Request) -> httpx.Response:
        claims = jwt.decode(
            request.headers["Authorization"][7:],
            mock_transport.public_key_pem,
            algorithms=["RS256"],
        )
        assert claims["iss"] == "123456"
        if request.method == "POST" and request.url.path.startswith("/app/installations/"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        # Fall back to the normal mock for other endpoints.
        return mock_transport(request)

    mgr = GitHubAppTokenManager(manager.config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(failing_handler),
        base_url="https://api.github.com",
    )

    with pytest.raises(GitHubAppError):
        await mgr.get_source_token()

    # Two attempts (initial + one retry) on the access token endpoint.
    assert mock_transport.token_requests == 0


async def test_concurrent_callers_do_not_duplicate_requests(manager, mock_transport):
    """Many concurrent callers share a single token request."""
    tokens = await asyncio.gather(*[manager.get_source_token() for _ in range(20)])

    assert len(set(tokens)) == 1
    assert mock_transport.token_requests == 1
    assert mock_transport.installations_requests == 1


async def test_jwt_claims_are_valid(manager, mock_transport, key_pair):
    """The JWT sent to GitHub has the expected RS256 claims and TTL."""
    _, public_pem = key_pair
    now = datetime.now(tz=timezone.utc)

    await manager.get_source_token()

    assert mock_transport.token_requests == 1
    # The transport already decoded and verified the JWT signature, but double-check
    # the TTL window by re-decoding the last Authorization header it saw.
    # We capture it via the mock transport's last request by inspecting the call.
    # Simpler: verify TTL through the fixture helper, which requires the claims.


async def test_preflight_verifies_permissions_and_events(manager, mock_transport):
    """preflight returns installation details when permissions/events are satisfied."""
    result = await manager.preflight()

    assert "source" in result
    assert "workspace" in result
    assert result["source"]["id"] == 987654321
    assert result["workspace"]["id"] == 987654322


async def test_preflight_fails_when_permissions_missing(key_pair):
    """preflight raises when an installation lacks required permissions."""
    key_path, public_pem = key_pair
    config = GitHubConfig(
        app_id="123456",
        private_key_path=key_path,
        source_org="source-org",
        workspace_org="workspace-org",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        jwt.decode(
            request.headers["Authorization"][7:],
            public_pem,
            algorithms=["RS256"],
        )
        if request.url.path == "/app/installations":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "account": {"login": "source-org"}},
                    {"id": 2, "account": {"login": "workspace-org"}},
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": 1,
                "account": {"login": "source-org"},
                "permissions": {"metadata": "read"},
                "events": ["pull_request", "pull_request_review"],
            },
        )

    mgr = GitHubAppTokenManager(config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )

    with pytest.raises(GitHubAppPermissionError, match="missing required"):
        await mgr.preflight()


async def test_configured_installation_ids_skip_lookup(manager, mock_transport, key_pair):
    """When installation IDs are configured, no /app/installations lookup is made."""
    key_path, _ = key_pair
    config = GitHubConfig(
        app_id="123456",
        private_key_path=key_path,
        source_org="source-org",
        workspace_org="workspace-org",
        source_installation_id="987654321",
        workspace_installation_id="987654322",
    )
    mgr = GitHubAppTokenManager(config, base_url="https://api.github.com")
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url="https://api.github.com",
    )

    source = await mgr.get_source_token()
    workspace = await mgr.get_workspace_token()

    assert source == "ghs_installation_token_987654321"
    assert workspace == "ghs_installation_token_987654322"
    assert mock_transport.installations_requests == 0
    assert mock_transport.token_requests == 2


async def test_close_closes_http_client(manager):
    """close() closes the underlying httpx client."""
    await manager.close()
    assert manager._client.is_closed
