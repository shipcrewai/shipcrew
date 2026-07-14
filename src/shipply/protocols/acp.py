"""Pydantic models for the Agent Client Protocol (ACP) JSON-RPC 2.0 messages.

These models cover the verified ACP contract used by Oh My Pi v16.3.3 over
stdio. The harness speaks JSON-RPC 2.0 requests, responses, and notifications,
with the concrete method shapes defined here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ACPError(BaseModel):
    """JSON-RPC 2.0 error object returned by the agent."""

    model_config = ConfigDict(extra="allow")

    code: int
    message: str
    data: Any | None = None


class ACPRequest(BaseModel):
    """Outgoing JSON-RPC 2.0 request from the harness to the agent."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: Any = None


class ACPResponse(BaseModel):
    """Raw JSON-RPC 2.0 response helper.

    Use :meth:`raise_for_error` to translate a JSON-RPC error into a runtime
    exception, and :meth:`get_result` to retrieve the result payload.
    """

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any | None = None
    error: ACPError | None = None

    def raise_for_error(self) -> None:
        """Raise ``RuntimeError`` if this response carries a JSON-RPC error."""
        if self.error is not None:
            raise RuntimeError(f"ACP error {self.error.code}: {self.error.message}")

    def get_result(self) -> Any:
        """Return the ``result`` field, raising if an error is present."""
        self.raise_for_error()
        return self.result


class ACPNotification(BaseModel):
    """Inbound JSON-RPC 2.0 notification (no ``id``)."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: Any = None


class TextContentBlock(BaseModel):
    """Text content block sent inside a ``session/prompt`` prompt."""

    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str


class InitializeRequest(BaseModel):
    """Client → agent ``initialize`` request params."""

    model_config = ConfigDict(extra="allow")

    protocolVersion: int = 1


class InitializeResponse(BaseModel):
    """Agent → client ``initialize`` response result."""

    model_config = ConfigDict(extra="allow")

    protocolVersion: int
    agentInfo: dict[str, Any] = Field(default_factory=dict)
    agentCapabilities: dict[str, Any] = Field(default_factory=dict)
    authMethods: list[str] = Field(default_factory=list)


class AuthenticateRequest(BaseModel):
    """Client → agent ``authenticate`` request params.

    For headless bots, ``method`` should be ``"agent"`` to reuse provider keys
    and OAuth state under ``~/.omp``. ``"terminal"`` launches the TUI and is
    not appropriate for a bot process.
    """

    model_config = ConfigDict(extra="allow")

    method: str = "agent"
    options: dict[str, Any] = Field(default_factory=dict)


class AuthenticateResponse(BaseModel):
    """Agent → client ``authenticate`` response result."""

    model_config = ConfigDict(extra="allow")

    success: bool = True
    method: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionNewRequest(BaseModel):
    """Client → agent ``session/new`` request params."""

    model_config = ConfigDict(extra="allow")

    cwd: str | None = None
    mcpServers: list[dict[str, Any]] = Field(default_factory=list)


class SessionNewResponse(BaseModel):
    """Agent → client ``session/new`` response result."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    configOptions: dict[str, Any] = Field(default_factory=dict)


class SessionPromptRequest(BaseModel):
    """Client → agent ``session/prompt`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    prompt: list[TextContentBlock | dict[str, Any]] = Field(default_factory=list)


class SessionPromptResponse(BaseModel):
    """Agent → client ``session/prompt`` response result."""

    model_config = ConfigDict(extra="allow")

    stopReason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class SessionUpdateNotification(BaseModel):
    """Agent → client ``session/update`` notification params.

    The ``type`` field determines which additional fields are present:

    * ``agent_message_chunk`` — streaming final output (``text`` / ``content``)
    * ``agent_thought_chunk`` — streaming reasoning (``text``)
    * ``tool_call`` / ``tool_call_update`` — tool invocation (``tool_call``)
    * ``usage_update`` — token/cost usage (``usage``)
    * ``session_info_update`` — session metadata changes (``session_info``)
    * ``current_mode_update`` / ``config_option_update`` — mode/config changes
    """

    model_config = ConfigDict(extra="allow")

    sessionId: str
    type: str


class PermissionRequest(BaseModel):
    """Agent → client ``session/request_permission`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    tool_name: str | None = None
    description: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class FsReadRequest(BaseModel):
    """Agent → client ``fs/read_text_file`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    path: str


class FsWriteRequest(BaseModel):
    """Agent → client ``fs/write_text_file`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    path: str
    content: str | None = None


class TerminalCreateRequest(BaseModel):
    """Agent → client ``terminal/create`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    command: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


class TerminalOutputRequest(BaseModel):
    """Agent → client ``terminal/output`` request params."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    terminal_id: str | None = None
    output: str | None = None


class HarnessResult(BaseModel):
    """Structured result returned by ``HarnessBackend`` to bot handlers.

    ``status`` is one of ``success``, ``needs_input``, or ``error``.
    ``payload`` carries the result data, the full agent text, or an error detail
    object (e.g. ``{"message": "...", "raw": "..."}``).
    """

    model_config = ConfigDict(extra="allow")

    status: Literal["success", "needs_input", "error"]
    payload: Any = None
