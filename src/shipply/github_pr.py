"""Fork-based PR creation service for Shipply proposals."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple

import httpx

from shipply.github_app import GitHubAppTokenManager
from shipply.github_preflight import (
    FORK_CREATE_TIMEOUT_SECONDS,
    FORK_POLL_INTERVAL_SECONDS,
    GITHUB_API_VERSION,
)

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubPRError(Exception):
    """Raised when a fork-based PR operation fails."""


class _GitRun(NamedTuple):
    """Minimal stand-in for a completed subprocess run."""

    returncode: int
    stdout: bytes
    stderr: bytes


class ForkBasedPRService:
    """Create or update pull requests from a workspace-org fork.

    The service uses a GitHub App installation token manager to obtain
    short-lived tokens for both the source organization (read PRs, open PRs)
    and the workspace organization (create/read forks, push branches).  It
    relies on deterministic branch names ``shipply/{proposal_id}`` so that
    repeated calls for the same proposal are idempotent.
    """

    def __init__(
        self,
        token_manager: GitHubAppTokenManager,
        repo_scope: list[str],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_manager = token_manager
        self.repo_scope = [item.strip() for item in repo_scope if item.strip()]
        self._client = client or httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the HTTP client and the underlying token manager."""
        await self._client.aclose()
        await self.token_manager.close()

    def _repo_in_scope(self, source_repo: str) -> bool:
        """Return ``True`` when ``source_repo`` matches the configured scope.

        Scope items may be ``owner/repo`` or just ``repo``.  Comparisons are
        case-insensitive.
        """
        if not source_repo or "/" not in source_repo:
            return False
        repo_part = source_repo.split("/")[-1].lower()
        for item in self.repo_scope:
            if "/" in item:
                if item.lower() == source_repo.lower():
                    return True
            elif item.lower() == repo_part.lower():
                return True
        return False

    async def _api(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {token}",
        }
        return await self._client.request(
            method,
            path,
            headers=headers,
            params=params,
            json=json,
        )

    async def _ensure_fork(
        self,
        source_org: str,
        repo: str,
        workspace_org: str,
        workspace_token: str,
    ) -> None:
        """Ensure the workspace-org fork exists, creating and polling if needed."""
        path = f"/repos/{workspace_org}/{repo}"
        response = await self._api("GET", path, workspace_token)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            _raise_for_status(
                response,
                f"checking workspace fork {workspace_org}/{repo}",
            )

        response = await self._api(
            "POST",
            f"/repos/{source_org}/{repo}/forks",
            workspace_token,
            json={
                "organization": workspace_org,
                "name": repo,
                "default_branch_only": True,
            },
        )
        if response.status_code == 403:
            raise GitHubPRError(
                f"Workspace token cannot create fork {workspace_org}/{repo}; "
                "ensure the workspace org allows forking and the GitHub App has "
                "administration:write permission"
            )
        if response.status_code not in (200, 202):
            _raise_for_status(
                response,
                f"creating fork {workspace_org}/{repo} of {source_org}/{repo}",
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + FORK_CREATE_TIMEOUT_SECONDS
        while True:
            await asyncio.sleep(FORK_POLL_INTERVAL_SECONDS)
            response = await self._api("GET", path, workspace_token)
            if response.status_code == 200:
                return
            if loop.time() > deadline:
                raise GitHubPRError(
                    f"Timed out after {FORK_CREATE_TIMEOUT_SECONDS}s waiting for "
                    f"fork {workspace_org}/{repo} to be created"
                )

    async def _run_git(self, *args: str) -> _GitRun:
        """Run a ``git`` subprocess and return its exit status and output."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return _GitRun(proc.returncode or 0, stdout, stderr)

    async def _push_branch(
        self,
        source_org: str,
        repo: str,
        workspace_org: str,
        branch: str,
        workspace_token: str,
    ) -> None:
        """Push ``branch`` to the workspace fork."""
        remote_url = (
            f"https://x-access-token:{workspace_token}@github.com/"
            f"{workspace_org}/{repo}.git"
        )
        result = await self._run_git("push", remote_url, f"{branch}:{branch}")
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise GitHubPRError(
                f"Failed to push branch {branch} to {workspace_org}/{repo}: {stderr}"
            )

    async def _find_existing_pr(
        self,
        source_org: str,
        repo: str,
        workspace_org: str,
        branch: str,
        source_token: str,
    ) -> int | None:
        """Return the number of an open PR from ``branch`` or ``None``."""
        params = {"state": "open", "head": f"{workspace_org}:{branch}"}
        response = await self._api(
            "GET",
            f"/repos/{source_org}/{repo}/pulls",
            source_token,
            params=params,
        )
        if response.status_code == 403:
            raise GitHubPRError(
                "Source-org token cannot list pull requests; "
                "ensure pull_requests:write is granted"
            )
        if response.status_code != 200:
            _raise_for_status(
                response,
                f"listing pull requests on {source_org}/{repo}",
            )
        items = response.json()
        if items:
            return int(items[0]["number"])
        return None

    async def _default_branch(
        self, source_org: str, repo: str, source_token: str
    ) -> str:
        """Return the source repository's default branch, falling back to ``main``."""
        response = await self._api(
            "GET", f"/repos/{source_org}/{repo}", source_token
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("default_branch") or "main"
        return "main"

    async def _create_or_update_pr(
        self,
        source_org: str,
        repo: str,
        workspace_org: str,
        branch: str,
        title: str,
        body: str,
        source_token: str,
    ) -> str:
        """Create or patch a PR on the source repo and return its URL."""
        number = await self._find_existing_pr(
            source_org, repo, workspace_org, branch, source_token
        )
        if number is not None:
            response = await self._api(
                "PATCH",
                f"/repos/{source_org}/{repo}/pulls/{number}",
                source_token,
                json={"title": title, "body": body},
            )
            if response.status_code == 403:
                raise GitHubPRError(
                    "Source-org token cannot update pull requests; "
                    "ensure pull_requests:write is granted"
                )
            if response.status_code != 200:
                _raise_for_status(
                    response,
                    f"updating pull request #{number} on {source_org}/{repo}",
                )
            data = response.json()
        else:
            base = await self._default_branch(source_org, repo, source_token)
            response = await self._api(
                "POST",
                f"/repos/{source_org}/{repo}/pulls",
                source_token,
                json={
                    "title": title,
                    "body": body,
                    "head": f"{workspace_org}:{branch}",
                    "base": base,
                },
            )
            if response.status_code == 403:
                raise GitHubPRError(
                    "Source-org token cannot create pull requests; "
                    "ensure pull_requests:write is granted"
                )
            if response.status_code != 201:
                _raise_for_status(
                    response,
                    f"creating pull request on {source_org}/{repo}",
                )
            data = response.json()
        return str(data["html_url"])

    async def create_or_update_pr(
        self,
        source_repo: str,
        proposal_id: str,
        title: str,
        body: str,
    ) -> str:
        """Create or update a PR from the workspace fork for ``proposal_id``.

        Args:
            source_repo: Source repository as ``owner/repo``.
            proposal_id: The Shipply proposal identifier.
            title: Pull request title.
            body: Pull request body.

        Returns:
            The public URL of the pull request.

        Raises:
            GitHubPRError: If the repo is out of scope, the fork cannot be
                created, the branch cannot be pushed, or a PR cannot be
                opened/updated.
        """
        if not self._repo_in_scope(source_repo):
            raise GitHubPRError(
                f"Repository {source_repo} is not within the configured repo_scope"
            )

        if "/" not in source_repo:
            raise GitHubPRError(f"source_repo must be owner/repo, got {source_repo!r}")
        source_org, repo = source_repo.split("/", 1)
        workspace_org = self.token_manager.config.workspace_org
        if not workspace_org:
            raise GitHubPRError("github.workspace_org is not configured")

        source_token = await self.token_manager.get_source_token()
        workspace_token = await self.token_manager.get_workspace_token()

        branch = f"shipply/{proposal_id}"
        await self._ensure_fork(source_org, repo, workspace_org, workspace_token)
        await self._push_branch(
            source_org, repo, workspace_org, branch, workspace_token
        )
        return await self._create_or_update_pr(
            source_org, repo, workspace_org, branch, title, body, source_token
        )


def _raise_for_status(response: httpx.Response, action: str) -> None:
    """Raise a descriptive ``GitHubPRError`` for a non-2xx response."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubPRError(
            f"GitHub API returned {exc.response.status_code} {exc.response.reason_phrase} "
            f"while {action}"
        ) from exc
