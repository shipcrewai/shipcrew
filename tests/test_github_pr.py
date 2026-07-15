"""Tests for the fork-based PR service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shipply.config import GitHubConfig
from shipply.github_pr import ForkBasedPRService, GitHubPRError, _GitRun


GITHUB_API_BASE = "https://api.github.com"


def make_service(
    repo_scope: list[str],
    route_handler,
    source_org: str = "source-org",
    workspace_org: str = "workspace-org",
) -> tuple[ForkBasedPRService, MagicMock]:
    """Build a PR service with a mocked token manager and a custom HTTP transport."""
    token_manager = MagicMock()
    token_manager.config = GitHubConfig(
        source_org=source_org,
        workspace_org=workspace_org,
    )
    token_manager.get_source_token = AsyncMock(return_value="source-token")
    token_manager.get_workspace_token = AsyncMock(return_value="workspace-token")
    token_manager.close = AsyncMock()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(route_handler),
        base_url=GITHUB_API_BASE,
    )
    service = ForkBasedPRService(token_manager, repo_scope, client=client)
    return service, token_manager


def mock_git_push(service: ForkBasedPRService) -> AsyncMock:
    """Replace the git runner with a successful no-op."""
    runner = AsyncMock(return_value=_GitRun(0, b"", b""))
    service._run_git = runner
    return runner


@pytest.fixture(autouse=True)
def fast_fork_poll(monkeypatch) -> None:
    """Speed up fork-creation polling in tests."""
    monkeypatch.setattr("shipply.github_pr.FORK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("shipply.github_pr.FORK_CREATE_TIMEOUT_SECONDS", 1)


async def test_create_new_pr_from_existing_fork() -> None:
    """A new PR is opened when the fork exists and no matching PR is open."""
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            return httpx.Response(200, json={"full_name": "workspace-org/repo"})
        if request.method == "GET" and request.url.path == "/repos/source-org/repo/pulls":
            assert request.url.params["state"] == "open"
            assert request.url.params["head"] == "workspace-org:shipply/prop-1"
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/source-org/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/source-org/repo/pulls":
            return httpx.Response(
                201,
                json={
                    "number": 1,
                    "html_url": "https://github.com/source-org/repo/pull/1",
                },
            )
        return httpx.Response(404)

    service, _ = make_service(["source-org/repo"], handler)
    runner = mock_git_push(service)

    url = await service.create_or_update_pr(
        "source-org/repo", "prop-1", "Add feature", "Body text"
    )

    assert url == "https://github.com/source-org/repo/pull/1"
    runner.assert_awaited_once_with(
        "push",
        "https://x-access-token:workspace-token@github.com/workspace-org/repo.git",
        "shipply/prop-1:shipply/prop-1",
    )
    assert ("POST", "/repos/source-org/repo/pulls") in requests

    await service.close()


async def test_update_existing_open_pr() -> None:
    """An existing open PR from the same branch has its title/body updated."""
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            return httpx.Response(200, json={"full_name": "workspace-org/repo"})
        if request.method == "GET" and request.url.path == "/repos/source-org/repo/pulls":
            assert request.url.params["head"] == "workspace-org:shipply/prop-2"
            return httpx.Response(200, json=[{"number": 7}])
        if request.method == "PATCH" and request.url.path == "/repos/source-org/repo/pulls/7":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "html_url": "https://github.com/source-org/repo/pull/7",
                },
            )
        return httpx.Response(404)

    # Bare repo name in scope should still match source-org/repo.
    service, _ = make_service(["repo"], handler)
    runner = mock_git_push(service)

    url = await service.create_or_update_pr(
        "source-org/repo", "prop-2", "Updated title", "Updated body"
    )

    assert url == "https://github.com/source-org/repo/pull/7"
    runner.assert_awaited_once_with(
        "push",
        "https://x-access-token:workspace-token@github.com/workspace-org/repo.git",
        "shipply/prop-2:shipply/prop-2",
    )
    assert ("PATCH", "/repos/source-org/repo/pulls/7") in requests

    await service.close()


async def test_out_of_scope_repo_dropped_without_api_calls() -> None:
    """Out-of-scope repositories are rejected before any token or API use."""
    api_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append(request.url.path)
        return httpx.Response(404)

    service, token_manager = make_service(["source-org/repo"], handler)
    service._run_git = AsyncMock()

    with pytest.raises(GitHubPRError, match="not within the configured repo_scope"):
        await service.create_or_update_pr(
            "other-org/repo", "prop-1", "Title", "Body"
        )

    assert not api_calls
    token_manager.get_source_token.assert_not_called()
    token_manager.get_workspace_token.assert_not_called()

    await service.close()


async def test_missing_workspace_fork_is_created_and_polled() -> None:
    """When the fork is missing the service creates it and polls for readiness."""
    state = {"fork_exists": False}
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            if state["fork_exists"]:
                return httpx.Response(200)
            state["fork_exists"] = True
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/repos/source-org/repo/forks":
            return httpx.Response(202)
        if request.method == "GET" and request.url.path == "/repos/source-org/repo/pulls":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/source-org/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/source-org/repo/pulls":
            return httpx.Response(
                201,
                json={
                    "number": 2,
                    "html_url": "https://github.com/source-org/repo/pull/2",
                },
            )
        return httpx.Response(404)

    service, _ = make_service(["source-org/repo"], handler)
    mock_git_push(service)

    url = await service.create_or_update_pr(
        "source-org/repo", "prop-3", "Title", "Body"
    )

    assert url == "https://github.com/source-org/repo/pull/2"
    assert ("POST", "/repos/source-org/repo/forks") in requests

    await service.close()


async def test_branch_push_failure_raises() -> None:
    """A failed git push is surfaced as a clear GitHubPRError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            return httpx.Response(200)
        return httpx.Response(404)

    service, _ = make_service(["source-org/repo"], handler)
    service._run_git = AsyncMock(
        return_value=_GitRun(1, b"", b"fatal: could not resolve branch")
    )

    with pytest.raises(GitHubPRError, match="Failed to push branch"):
        await service.create_or_update_pr(
            "source-org/repo", "prop-4", "Title", "Body"
        )

    await service.close()


async def test_source_token_lacking_pr_write_raises() -> None:
    """A 403 from the source org when creating a PR is mapped to a clear error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/repos/source-org/repo/pulls":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/source-org/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/source-org/repo/pulls":
            return httpx.Response(
                403,
                json={"message": "Resource not accessible by integration"},
            )
        return httpx.Response(404)

    service, _ = make_service(["source-org/repo"], handler)
    mock_git_push(service)

    with pytest.raises(GitHubPRError, match="cannot create pull requests"):
        await service.create_or_update_pr(
            "source-org/repo", "prop-5", "Title", "Body"
        )

    await service.close()


async def test_branch_name_is_deterministic() -> None:
    """The same proposal always pushes the same branch name."""
    state = {"created": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/workspace-org/repo":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/repos/source-org/repo/pulls":
            if state["created"]:
                return httpx.Response(200, json=[{"number": 1}])
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/source-org/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/source-org/repo/pulls":
            state["created"] = True
            return httpx.Response(
                201,
                json={
                    "number": 1,
                    "html_url": "https://github.com/source-org/repo/pull/1",
                },
            )
        if request.method == "PATCH" and request.url.path == "/repos/source-org/repo/pulls/1":
            return httpx.Response(
                200,
                json={
                    "number": 1,
                    "html_url": "https://github.com/source-org/repo/pull/1",
                },
            )
        return httpx.Response(404)

    service, _ = make_service(["source-org/repo"], handler)
    runner = mock_git_push(service)

    await service.create_or_update_pr(
        "source-org/repo", "prop-d", "Title", "Body"
    )
    await service.create_or_update_pr(
        "source-org/repo", "prop-d", "Title 2", "Body 2"
    )

    assert len(runner.call_args_list) == 2
    for call in runner.call_args_list:
        assert call.args[2] == "shipply/prop-d:shipply/prop-d"

    await service.close()
