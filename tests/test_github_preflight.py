"""Tests for shipply.github_preflight."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest

from shipply.config import GitHubConfig
from shipply.github_preflight import (
    PreflightError,
    _load_github_config,
    check_fork_capability,
    main,
)


def _config(**kwargs: Any) -> GitHubConfig:
    defaults = {
        "source_org": "src-org",
        "workspace_org": "ws-org",
        "repo_scope": ["repo1"],
    }
    defaults.update(kwargs)
    return GitHubConfig(**defaults)


def _response(status_code: int = 200, json: dict[str, Any] | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json or {}
    resp.text = ""
    return resp


@pytest.fixture
def mock_client():
    """Patch httpx.Client so tests can drive API responses."""
    with patch("shipply.github_preflight.httpx.Client") as client_cls:
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client_cls.return_value = client
        yield client


def _build_get_side_effect(
    *,
    source_org: int = 200,
    workspace_org: int = 200,
    permissions: dict[str, Any] | None = None,
    source_repo: tuple[int, dict[str, Any]] = (200, {"private": True}),
    fork: tuple[int, dict[str, Any]] = (404, {}),
):
    """Return a side-effect function for client.get."""
    permissions = permissions or {"administration": "write"}

    def side_effect(url: str, **kwargs: Any) -> Mock:
        if url.endswith("/orgs/src-org") or "/orgs/src-org" in url:
            return _response(source_org)
        if url.endswith("/orgs/ws-org") or "/orgs/ws-org" in url:
            return _response(workspace_org)
        if "/installation/repositories" in url:
            return _response(200, {"permissions": permissions})
        if f"/repos/src-org/repo1" in url:
            return _response(source_repo[0], source_repo[1])
        if f"/repos/ws-org/repo1" in url:
            return _response(fork[0], fork[1])
        return _response(404)

    return side_effect


class TestCheckForkCapability:
    def test_success_creates_fork(self, mock_client: Mock) -> None:
        fork_checks: list[int] = []

        def get_side_effect(url: str, **kwargs: Any) -> Mock:
            if "/repos/ws-org/repo1" in url:
                fork_checks.append(1)
                if len(fork_checks) == 1:
                    return _response(404)
                return _response(200, {"private": True})
            if "/orgs/src-org" in url:
                return _response(200)
            if "/orgs/ws-org" in url:
                return _response(200)
            if "/installation/repositories" in url:
                return _response(200, {"permissions": {"administration": "write"}})
            if "/repos/src-org/repo1" in url:
                return _response(200, {"private": True})
            return _response(404)

        mock_client.get.side_effect = get_side_effect
        mock_client.post.side_effect = lambda url, **kwargs: (
            _response(202) if "/forks" in url else _response(404)
        )

        check_fork_capability(_config(), "token")

        assert mock_client.post.call_count == 1
        assert fork_checks[-1] == 1  # poll found the fork

    def test_success_existing_fork(self, mock_client: Mock) -> None:
        mock_client.get.side_effect = _build_get_side_effect(
            fork=(200, {"private": True})
        )

        check_fork_capability(_config(), "token")

        mock_client.post.assert_not_called()

    def test_missing_source_repo(self, mock_client: Mock) -> None:
        mock_client.get.side_effect = _build_get_side_effect(
            source_repo=(404, {})
        )

        with pytest.raises(PreflightError) as exc_info:
            check_fork_capability(_config(), "token")

        assert "Source repository" in str(exc_info.value)
        assert "repo1" in str(exc_info.value)

    def test_missing_workspace_permission(self, mock_client: Mock) -> None:
        mock_client.get.side_effect = _build_get_side_effect(
            permissions={"administration": "read"}
        )

        with pytest.raises(PreflightError) as exc_info:
            check_fork_capability(_config(), "token")

        assert "administration:write" in str(exc_info.value)
        assert "administration='read'" in str(exc_info.value)

    def test_public_fork_when_source_is_private(self, mock_client: Mock) -> None:
        mock_client.get.side_effect = _build_get_side_effect(
            source_repo=(200, {"private": True}),
            fork=(200, {"private": False}),
        )

        with pytest.raises(PreflightError) as exc_info:
            check_fork_capability(_config(), "token")

        assert "public" in str(exc_info.value)
        assert "private" in str(exc_info.value)

    def test_network_failure(self, mock_client: Mock) -> None:
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with pytest.raises(PreflightError) as exc_info:
            check_fork_capability(_config(), "token")

        assert "Network error" in str(exc_info.value)


class TestMainCLI:
    def test_main_exits_1_without_token(self, capsys, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.err

    def test_main_exits_0_on_success(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "workspace-token")

        with patch(
            "shipply.github_preflight._load_github_config", return_value=_config()
        ), patch(
            "shipply.github_preflight.check_fork_capability"
        ) as check:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        check.assert_called_once_with(_config(), "workspace-token")

    def test_main_exits_1_on_preflight_failure(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "workspace-token")

        with patch(
            "shipply.github_preflight._load_github_config", return_value=_config()
        ), patch(
            "shipply.github_preflight.check_fork_capability",
            side_effect=PreflightError("workspace org blocked"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1


class TestLoadGitHubConfig:
    def test_fallback_reads_raw_github_table(self, tmp_path, monkeypatch) -> None:
        """If ShipplyConfig lacks a github attribute, the CLI falls back to raw TOML."""
        config_path = tmp_path / "shipply.toml"
        config_path.write_text(
            """
[personas.scout]
model = "fast"

[personas.doc-review]
model = "fast"

[personas.blueprint]
model = "default"

[personas.forge]
model = "default"

[personas.gate-1]
model = "slow"

[personas.gate-2]
model = "slow"

[personas.gate-3]
model = "slow"

[gates.gate-1]
quorum = 1

[gates.gate-2]
quorum = 1

[gates.gate-3]
quorum = 1

[github]
source_org = "fallback-src"
workspace_org = "fallback-ws"
repo_scope = ["repo-a"]
"""
        )
        monkeypatch.setenv("SHIPPLY_CONFIG", str(config_path))

        github_config = _load_github_config()

        assert github_config.source_org == "fallback-src"
        assert github_config.workspace_org == "fallback-ws"
        assert github_config.repo_scope == ["repo-a"]
