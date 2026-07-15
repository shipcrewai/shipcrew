"""GitHub App JWT and installation token manager."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt

from shipply.config import GitHubConfig

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
SOURCE_REQUIRED_PERMISSIONS = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "write",
}
SOURCE_REQUIRED_EVENTS = {"pull_request", "pull_request_review"}
WORKSPACE_REQUIRED_PERMISSIONS = {
    "administration": "write",
    "contents": "write",
}
WORKSPACE_REQUIRED_EVENTS: set[str] = set()

DEFAULT_JWT_TTL_MINUTES = 10
DEFAULT_TOKEN_SAFETY_MARGIN = timedelta(minutes=5)


class GitHubAppError(Exception):
    """Base exception for GitHub App token manager errors."""


class GitHubAppConfigError(GitHubAppError):
    """Raised when required configuration is missing or invalid."""


class GitHubAppInstallationError(GitHubAppError):
    """Raised when an installation cannot be found or is not usable."""


class GitHubAppPermissionError(GitHubAppError):
    """Raised when an installation lacks required permissions or events."""


class GitHubAppTokenManager:
    """Manage GitHub App JWTs and installation access tokens."""

    def __init__(
        self,
        config: GitHubConfig,
        *,
        base_url: str = GITHUB_API_BASE,
        jwt_ttl_minutes: int = DEFAULT_JWT_TTL_MINUTES,
        safety_margin: timedelta = DEFAULT_TOKEN_SAFETY_MARGIN,
    ) -> None:
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.jwt_ttl = timedelta(minutes=jwt_ttl_minutes)
        self.safety_margin = safety_margin
        self._validate_config()
        self._private_key = self._load_private_key()
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    def _validate_config(self) -> None:
        """Validate that the GitHub App configuration is sufficiently present."""
        if not self.config.app_id:
            raise GitHubAppConfigError("GitHub App ID is not configured")

    def _load_private_key(self) -> str:
        """Load the PEM private key from the configured path or environment."""
        path = self.config.private_key_path or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        if not path:
            raise GitHubAppConfigError(
                "GitHub App private key path not configured; "
                "set github.private_key_path in shipply.toml or GITHUB_APP_PRIVATE_KEY_PATH"
            )
        if not os.path.isfile(path):
            raise GitHubAppConfigError(f"GitHub App private key not found at {path}")
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read()
        if "BEGIN" not in key or "PRIVATE KEY" not in key:
            raise GitHubAppConfigError(
                "GitHub App private key does not appear to be a valid PEM file"
            )
        return key

    def _create_jwt(self) -> str:
        """Create a short-lived RS256 JWT signed with the App private key."""
        if not self.config.app_id:
            raise GitHubAppConfigError("GitHub App ID is not configured")
        now = datetime.now(tz=timezone.utc)
        payload = {
            "iat": int(now.timestamp()),
            "exp": int((now + self.jwt_ttl).timestamp()),
            "iss": self.config.app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        use_jwt: bool = True,
        token: str | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Make an authenticated GitHub API request.

        When ``use_jwt`` is True the Authorization header carries a freshly
        generated JWT. Otherwise ``token`` is used as a Bearer token.
        """
        headers: dict[str, str] = {}
        if use_jwt:
            headers["Authorization"] = f"Bearer {self._create_jwt()}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            raise GitHubAppConfigError("No authorization token available for request")

        request = self._client.build_request(method, path, headers=headers, json=json)
        return await self._client.send(request)

    async def _get_installation_id(self, org: str, configured_id: str | None) -> str:
        """Resolve an installation ID, looking it up via GitHub if needed."""
        if configured_id:
            return configured_id
        if not org:
            raise GitHubAppConfigError("GitHub organization is not configured")
        response = await self._request("GET", "/app/installations")
        if response.status_code == 404:
            raise GitHubAppInstallationError(
                f"GitHub App is not installed for organization {org!r}"
            )
        response.raise_for_status()
        for installation in response.json():
            account = installation.get("account") or {}
            if account.get("login") == org:
                return str(installation["id"])
        raise GitHubAppInstallationError(
            f"GitHub App installation not found for organization {org!r}"
        )

    async def _create_installation_token(
        self, key: str, installation_id: str
    ) -> tuple[str, datetime]:
        """Exchange a JWT for a one-hour installation access token.

        On 401/403 the cached token for ``key`` is invalidated and the request
        is retried once with a fresh JWT.
        """
        for attempt in range(2):
            response = await self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
            )
            if response.status_code == 201:
                data = response.json()
                token = data["token"]
                expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                return token, expires_at
            if response.status_code in (401, 403) and attempt == 0:
                logger.warning(
                    "GitHub API returned %s for %s; invalidating cached token and retrying once",
                    response.status_code,
                    key,
                )
                self._tokens.pop(key, None)
                continue
            if response.status_code == 404:
                raise GitHubAppInstallationError(
                    f"GitHub App installation {installation_id} not found"
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise GitHubAppError(
                    f"GitHub App token exchange failed for installation {installation_id}: "
                    f"{exc.response.status_code} {exc.response.reason_phrase}"
                ) from exc
        raise GitHubAppError(
            f"GitHub App token exchange failed for installation {installation_id} after retry"
        )

    async def _get_token(self, key: str, installation_id: str | None, org: str) -> str:
        """Return a cached installation token or fetch a fresh one."""
        async with self._lock:
            cached = self._tokens.get(key)
            if cached is not None:
                token, expires_at = cached
                if datetime.now(tz=timezone.utc) < expires_at - self.safety_margin:
                    return token
                # Token is within the safety margin; drop it so we refresh.
                self._tokens.pop(key, None)

            resolved_id = await self._get_installation_id(org, installation_id)
            token, expires_at = await self._create_installation_token(key, resolved_id)
            self._tokens[key] = (token, expires_at)
            return token

    async def get_source_token(self) -> str:
        """Return a valid installation token for the source organization."""
        return await self._get_token(
            "source",
            self.config.source_installation_id,
            self.config.source_org or "",
        )

    async def get_workspace_token(self) -> str:
        """Return a valid installation token for the workspace organization."""
        return await self._get_token(
            "workspace",
            self.config.workspace_installation_id,
            self.config.workspace_org or "",
        )

    async def preflight(self) -> dict[str, Any]:
        """Verify required permissions and event subscriptions for each installation.

        Returns a mapping of ``source`` and ``workspace`` to installation details.
        """
        source_id = await self._get_installation_id(
            self.config.source_org or "",
            self.config.source_installation_id,
        )
        workspace_id = await self._get_installation_id(
            self.config.workspace_org or "",
            self.config.workspace_installation_id,
        )

        results: dict[str, Any] = {}
        for key, installation_id in [("source", source_id), ("workspace", workspace_id)]:
            response = await self._request("GET", f"/app/installations/{installation_id}")
            if response.status_code in (401, 403):
                response = await self._request("GET", f"/app/installations/{installation_id}")
            if response.status_code == 404:
                raise GitHubAppInstallationError(
                    f"GitHub App installation {installation_id} for {key} not found"
                )
            response.raise_for_status()
            installation = response.json()
            self._verify_installation(key, installation)
            results[key] = installation
        return results

    def _verify_installation(self, key: str, installation: dict[str, Any]) -> None:
        """Check that an installation meets the required permissions and events."""
        if key == "source":
            required_permissions = SOURCE_REQUIRED_PERMISSIONS
            required_events = SOURCE_REQUIRED_EVENTS
        elif key == "workspace":
            required_permissions = WORKSPACE_REQUIRED_PERMISSIONS
            required_events = WORKSPACE_REQUIRED_EVENTS
        else:  # pragma: no cover
            raise GitHubAppError(f"Unknown installation key: {key}")

        permissions = installation.get("permissions") or {}
        events = set(installation.get("events") or [])

        missing_permissions: list[str] = []
        for perm, required_level in required_permissions.items():
            actual_level = permissions.get(perm)
            if not _permission_satisfies(actual_level, required_level):
                missing_permissions.append(
                    f"{perm}={required_level} (got {actual_level!r})"
                )

        missing_events = sorted(required_events - events)

        if missing_permissions or missing_events:
            raise GitHubAppPermissionError(
                f"Installation {key} missing required permissions/events: "
                f"permissions={missing_permissions}, events={missing_events}"
            )


def _permission_satisfies(actual: str | None, required: str) -> bool:
    """Return True if ``actual`` meets or exceeds ``required``.

    GitHub permission levels are ordered ``read < write < admin``.
    """
    if actual is None:
        return False
    levels = {"read": 1, "write": 2, "admin": 3}
    return levels.get(actual, 0) >= levels.get(required, 0)
