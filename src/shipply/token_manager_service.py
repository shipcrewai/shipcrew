"""HTTP token manager service for GitHub App installation tokens."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from shipply.config import GitHubConfig, load_config
from shipply.github_app import GitHubAppTokenManager

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_MANAGER_SECRET = ""


def _load_secret(env_var: str, file_env_var: str | None = None) -> str | None:
    """Return a secret from an env var or an optional file."""
    value = os.environ.get(env_var)
    if value:
        return value
    if file_env_var:
        path = os.environ.get(file_env_var)
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
    return None


def _create_token_manager() -> GitHubAppTokenManager:
    """Create a token manager from environment and configuration."""
    config_path = os.environ.get("SHIPPLY_CONFIG", "shipply.toml")
    config = load_config(config_path)
    return GitHubAppTokenManager(config.github)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the token manager lifecycle."""
    manager: GitHubAppTokenManager = app.state.manager
    try:
        yield
    finally:
        await manager.close()


def create_app(manager: GitHubAppTokenManager | None = None) -> FastAPI:
    """Create a FastAPI application that vends GitHub App tokens."""
    app = FastAPI(title="Shipply GitHub App Token Manager", lifespan=lifespan)
    app.state.manager = manager or _create_token_manager()
    app.state.secret_token = _load_secret(
        "GITHUB_TOKEN_MANAGER_SECRET", "GITHUB_TOKEN_MANAGER_SECRET_FILE"
    ) or DEFAULT_TOKEN_MANAGER_SECRET

    def _authorize_token(authorization: str | None) -> None:
        """Validate the bearer token when one is configured."""
        if not app.state.secret_token:
            return
        expected = f"Bearer {app.state.secret_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/token/source")
    async def token_source(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> JSONResponse:
        """Return a source-organization installation token."""
        _authorize_token(authorization)
        token = await app.state.manager.get_source_token()
        return JSONResponse(status_code=200, content={"token": token})

    @app.get("/token/workspace")
    async def token_workspace(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> JSONResponse:
        """Return a workspace-organization installation token."""
        _authorize_token(authorization)
        token = await app.state.manager.get_workspace_token()
        return JSONResponse(status_code=200, content={"token": token})

    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check for orchestrators."""
        return JSONResponse(status_code=200, content={"status": "ok"})

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8001)
