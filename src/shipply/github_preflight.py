"""GitHub App / fork pre-flight checks for Shipply deployments."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

from shipply.config import GitHubConfig

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
FORK_CREATE_TIMEOUT_SECONDS = 60
FORK_POLL_INTERVAL_SECONDS = 2


class PreflightError(Exception):
    """Raised when a GitHub fork-capability pre-flight check fails."""


class _GitHubAPIError(PreflightError):
    """Internal subclass for API-level failures."""


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _raise_for_status(
    response: httpx.Response,
    action: str,
    status_messages: dict[int, str] | None = None,
) -> None:
    if response.status_code == 200:
        return
    status_messages = status_messages or {}
    message = status_messages.get(
        response.status_code,
        f"GitHub API returned {response.status_code} while {action}",
    )
    raise _GitHubAPIError(message)


def _load_github_config() -> GitHubConfig:
    """Load the GitHub config from ``shipply.toml``.

    Falls back to parsing the raw TOML ``[github]`` table when the root
    Pydantic model has not yet been rebuilt to include the ``github`` field.
    """
    from shipply.config import load_config

    config = load_config()
    if hasattr(config, "github"):
        return config.github

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover (python < 3.11)
        import tomli as tomllib  # type: ignore[no-redef]

    path = os.environ.get("SHIPPLY_CONFIG", "shipply.toml")
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return GitHubConfig(**raw.get("github", {}))


def check_fork_capability(github_config: GitHubConfig, token: str) -> None:
    """Verify that the workspace org can create private forks from the source org.

    The check validates, in order:

    1. The source organization is reachable.
    2. The workspace organization is reachable.
    3. The workspace installation token has ``administration:write`` permission.
    4. A sample repository from the source org is reachable and can be forked
       (or already exists) in the workspace org.
    5. When the source repository is private, the fork is also private.

    Raises:
        PreflightError: with actionable text if any check fails.
    """
    if not github_config.source_org:
        raise PreflightError("github.source_org is not configured")
    if not github_config.workspace_org:
        raise PreflightError("github.workspace_org is not configured")
    if not github_config.repo_scope:
        raise PreflightError(
            "github.repo_scope is empty; at least one source repository is required"
        )

    source_repo = github_config.repo_scope[0]
    source_org = github_config.source_org
    workspace_org = github_config.workspace_org

    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            headers=_github_headers(token),
            timeout=30,
        ) as client:
            # 1. Source organization must be reachable.
            resp = client.get(f"/orgs/{source_org}")
            _raise_for_status(
                resp,
                "checking the source organization",
                {
                    404: (
                        f"Source organization '{source_org}' was not found or is not "
                        "accessible to the GitHub App installation"
                    ),
                },
            )

            # 2. Workspace organization must be reachable.
            resp = client.get(f"/orgs/{workspace_org}")
            _raise_for_status(
                resp,
                "checking the workspace organization",
                {
                    404: (
                        f"Workspace organization '{workspace_org}' was not found or "
                        "is not accessible to the GitHub App installation"
                    ),
                },
            )

            # 3. Workspace installation token must grant administration:write.
            resp = client.get("/installation/repositories")
            _raise_for_status(
                resp,
                "checking workspace installation permissions",
                {
                    401: "The GitHub token is invalid or expired",
                    404: "The installation token cannot access /installation/repositories",
                },
            )
            permissions = resp.json().get("permissions", {})
            if permissions.get("administration") != "write":
                raise PreflightError(
                    "Workspace installation token is missing the 'administration:write' "
                    f"permission (got administration={permissions.get('administration')!r}). "
                    "Update the GitHub App installation on the workspace organization and "
                    "grant the 'Administration' repository permission with read/write access."
                )

            # 4. Source repository must be reachable.
            resp = client.get(f"/repos/{source_org}/{source_repo}")
            _raise_for_status(
                resp,
                f"checking the source repository '{source_org}/{source_repo}'",
                {
                    404: (
                        f"Source repository '{source_org}/{source_repo}' was not found. "
                        "Add it to the source-org GitHub App installation repository scope."
                    ),
                },
            )
            source_repo_info: dict[str, Any] = resp.json()
            source_is_private = source_repo_info.get("private", False)

            # 5. Fork must exist or be creatable.
            resp = client.get(f"/repos/{workspace_org}/{source_repo}")
            if resp.status_code == 200:
                fork_info = resp.json()
                _assert_fork_visibility(fork_info, source_is_private, source_repo)
                return

            if resp.status_code != 404:
                _raise_for_status(
                    resp,
                    f"checking for workspace fork '{workspace_org}/{source_repo}'",
                )

            # Fork does not exist; try to create it.
            create_resp = client.post(
                f"/repos/{source_org}/{source_repo}/forks",
                json={
                    "organization": workspace_org,
                    "name": source_repo,
                    "default_branch_only": True,
                },
            )
            if create_resp.status_code == 403:
                raise PreflightError(
                    f"GitHub refused to create a fork of '{source_org}/{source_repo}' "
                    f"in '{workspace_org}'. Check that the workspace organization allows "
                    "forking of private/internal repositories and that the GitHub App has "
                    "'administration:write' on the workspace org."
                )
            if create_resp.status_code not in (200, 202):
                _raise_for_status(
                    create_resp,
                    f"creating a fork of '{source_org}/{source_repo}'",
                )

            # 6. Poll until the fork is ready, confirming visibility.
            deadline = time.time() + FORK_CREATE_TIMEOUT_SECONDS
            while time.time() < deadline:
                resp = client.get(f"/repos/{workspace_org}/{source_repo}")
                if resp.status_code == 200:
                    fork_info = resp.json()
                    _assert_fork_visibility(fork_info, source_is_private, source_repo)
                    return
                time.sleep(FORK_POLL_INTERVAL_SECONDS)

            raise PreflightError(
                f"Timed out after {FORK_CREATE_TIMEOUT_SECONDS}s waiting for GitHub to "
                f"create the fork '{workspace_org}/{source_repo}'"
            )

    except httpx.NetworkError as exc:
        raise PreflightError(
            f"Network error reaching the GitHub API at {GITHUB_API_BASE}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise PreflightError(
            f"Timed out while contacting the GitHub API at {GITHUB_API_BASE}: {exc}"
        ) from exc


def _assert_fork_visibility(
    fork_info: dict[str, Any], source_is_private: bool, repo_name: str
) -> None:
    if source_is_private and not fork_info.get("private"):
        raise PreflightError(
            f"Source repository '{repo_name}' is private, but the fork in the workspace "
            "organization is public. A public fork would expose private source code. "
            "Enable private forking to the workspace organization in the GitHub fork policy."
        )


def main() -> None:
    """CLI entry point for ``python -m shipply.github_preflight``."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "GITHUB_TOKEN environment variable is required. Set it to a GitHub App "
            "installation token for the workspace organization.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        github_config = _load_github_config()
    except FileNotFoundError as exc:
        print(f"Could not load shipply.toml: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover - malformed TOML / config validation
        print(f"Failed to parse GitHub configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        check_fork_capability(github_config, token)
    except PreflightError as exc:
        print(f"GitHub pre-flight failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"GitHub pre-flight passed: workspace org '{github_config.workspace_org}' can "
        f"fork source org '{github_config.source_org}'."
    )
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
