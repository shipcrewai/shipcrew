"""ACP harness backend for driving Oh My Pi over stdio.

``HarnessBackend`` spawns an Oh My Pi subprocess in ACP mode (``omp acp`` by
default), completes the JSON-RPC 2.0 handshake, and exposes a simple async API
for bot handlers. ``HarnessPool`` manages one backend per persona and restarts
stalled backends on failure.

The ACP contract implemented here is the verified Oh My Pi v16.3.3 contract:

1. ``initialize`` — capability exchange.
2. ``authenticate`` — headless ``agent`` auth method.
3. ``session/new`` — create a working session.
4. ``session/prompt`` — send a task and stream structured updates.

Agent → client methods are handled with a conservative v1 policy: reads inside
the working directory are approved, destructive operations are declined.

Result contract
---------------

The harness persona is expected to end every response with a fenced JSON block
containing ``{"status": "success|needs_input|error", "payload": ...}``. If the
fenced block is missing, a fallback heuristic classifies the response as
``needs_input`` when it looks question-like, otherwise ``success``. Malformed
JSON is returned as ``status="error"`` with the raw text preserved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shipply.protocols.acp import (
    ACPError,
    AuthenticateRequest,
    AuthenticateResponse,
    FsReadRequest,
    FsWriteRequest,
    HarnessResult,
    InitializeRequest,
    InitializeResponse,
    PermissionRequest,
    SessionNewRequest,
    SessionNewResponse,
    SessionPromptRequest,
    SessionPromptResponse,
    SessionUpdateNotification,
    TerminalCreateRequest,
    TextContentBlock,
)

logger = logging.getLogger(__name__)


class HarnessError(Exception):
    """Raised when the harness cannot complete a request."""


class _PermissionPolicy:
    """Conservative v1 policy for agent → client permission requests."""

    def __init__(self, workdir: str | os.PathLike[str]) -> None:
        self.workdir = Path(workdir).resolve()

    def _is_within_workdir(self, path: str | os.PathLike[str]) -> bool:
        """Return ``True`` if ``path`` is inside the configured working dir."""
        try:
            target = Path(path).resolve()
            return target == self.workdir or self.workdir in target.parents
        except Exception:
            return False

    def _extract_path(self, request: PermissionRequest) -> str | None:
        """Try to locate a filesystem path inside a permission request."""
        if request.arguments:
            path = request.arguments.get("path")
            if path:
                return str(path)
        return None

    def allowed(self, request: PermissionRequest) -> tuple[bool, str | None]:
        """Return ``(allowed, reason_or_none)`` for a permission request."""
        tool = (request.tool_name or "").lower()
        desc = (request.description or "").lower()

        read_tool = "read" in tool or "fs/read" in tool
        write_tool = "write" in tool or "fs/write" in tool
        bash_tool = any(
            keyword in tool or keyword in desc
            for keyword in ("bash", "terminal", "shell", "exec", "command")
        )
        destructive = any(
            keyword in tool or keyword in desc
            for keyword in (
                "delete",
                "remove",
                "destroy",
                "rm ",
                "write",
                "bash",
                "terminal",
            )
        )

        if bash_tool:
            return False, "terminal/bash execution is declined by policy"

        if read_tool:
            path = self._extract_path(request)
            if path and not self._is_within_workdir(path):
                return False, f"path {path} is outside the working directory"
            return True, None

        if write_tool:
            return False, "write operations are declined by policy"

        if destructive:
            return False, "destructive operation declined by policy"

        return False, "operation not allowed by default policy"


class HarnessBackend:
    """Manage one Oh My Pi ACP subprocess and expose a handler-friendly API.

    Parameters
    ----------
    binary:
        Command to spawn. Default: ``"omp"``.
    args:
        Arguments passed after ``binary``. Default: ``["acp"]`` (ACP mode).
    cwd:
        Working directory for the subprocess and for new ACP sessions. Defaults
        to the current working directory.
    timeout:
        Default request timeout in seconds. Default: ``300``.
    """

    def __init__(  # noqa: PLR0913
        self,
        binary: str = "omp",
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: float = 300.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._args = list(args) if args else ["acp"]
        self._cwd = str(cwd or os.getcwd())
        self._timeout = timeout
        self._env = {**os.environ, **(env or {})}

        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[Any] | None = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._session_id: str | None = None
        self._agent_info: dict[str, Any] = {}
        self._message_chunks: list[str] = []
        self._updates: list[dict[str, Any]] = []
        self._crashed = False

        self._lock = asyncio.Lock()
        self._chunk_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._prompt_lock = asyncio.Lock()
        self._policy = _PermissionPolicy(self._cwd)

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    async def start(self) -> None:
        """Start the subprocess and complete the ACP handshake.

        The handshake is ``initialize`` → ``authenticate`` (``agent`` method) →
        ``session/new``. On failure the subprocess is cleaned up and
        ``HarnessError`` is raised.
        """
        if self._process is not None:
            return

        self._crashed = False
        self._validate_env()
        cmd = [self._binary] + list(self._args)
        logger.info(
            "Starting ACP harness: %s",
            " ".join(shlex.quote(str(part)) for part in cmd),
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                self._binary,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
            )
        except Exception as exc:
            raise HarnessError(f"Failed to start harness process: {exc}") from exc

        self._reader = asyncio.create_task(self._read_loop(), name="acp-reader")
        if self._process.stderr is not None:
            asyncio.create_task(self._drain_stderr(), name="acp-stderr")

        try:
            await self._initialize()
            await self._authenticate()
            await self._session_new()
        except Exception:
            await self.shutdown()
            raise

    def _validate_env(self) -> None:
        """Fail fast if configured environment variables are inconsistent."""
        if "PI_CONFIG_DIR" in self._env:
            config_dir = Path(self._env["PI_CONFIG_DIR"])
            if not config_dir.is_dir():
                raise HarnessError(f"PI_CONFIG_DIR does not exist: {config_dir}")

        if "OMP_AUTH_BROKER_URL" in self._env:
            has_token = "OMP_AUTH_BROKER_TOKEN" in self._env
            has_token_file = "OMP_AUTH_BROKER_TOKEN_FILE" in self._env
            if not has_token and not has_token_file:
                raise HarnessError(
                    "OMP_AUTH_BROKER_URL is set but neither OMP_AUTH_BROKER_TOKEN "
                    "nor OMP_AUTH_BROKER_TOKEN_FILE is set"
                )

        if "PI_CODING_AGENT_DIR" in self._env:
            agent_dir = Path(self._env["PI_CODING_AGENT_DIR"])
            try:
                agent_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise HarnessError(
                    f"Cannot create PI_CODING_AGENT_DIR {agent_dir}: {exc}"
                ) from exc
            if not os.access(agent_dir, os.W_OK):
                raise HarnessError(f"PI_CODING_AGENT_DIR is not writable: {agent_dir}")

    async def shutdown(self) -> None:
        """Close the current session (if any) and terminate the subprocess."""
        if self._session_id is not None and self._process is not None:
            try:
                await self._send_request(
                    "session/close",
                    {"sessionId": self._session_id},
                    timeout=10.0,
                )
            except Exception:
                logger.debug("session/close failed during shutdown", exc_info=True)
            self._session_id = None

        if self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
            except Exception:
                pass
            self._process = None

        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None

        self._crashed = True

    async def restart(self) -> None:
        """Shutdown and restart the subprocess, completing a fresh handshake."""
        await self.shutdown()
        await self.start()

    # ---------------------------------------------------------------------=
    # Public API
    # ---------------------------------------------------------------------

    async def new_session(self, cwd: str | None = None) -> str:
        """Create a new ACP session and return its ``sessionId``.

        The new session becomes the default session for subsequent ``prompt``
        calls. The old session, if any, is left open and must be closed by the
        caller if desired.
        """
        return await self._session_new(cwd=cwd)

    async def prompt(
        self,
        text: str,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> HarnessResult:
        """Send a text prompt to the agent and return a structured result.

        Only one prompt is processed at a time per backend. Streaming
        ``agent_message_chunk`` notifications are collected and concatenated;
        the final fenced JSON block is extracted and parsed as a
        ``HarnessResult``. If no fenced block is present, the fallback
        heuristic is used.

        Parameters
        ----------
        text:
            The prompt text. Handlers usually send structured JSON here
            (``{"task": ..., "context": ...}``) or plain text.
        session_id:
            Target session. Defaults to the session created during ``start()``
            or the most recent ``new_session()`` call.
        timeout:
            Override the default timeout for this request.

        Raises
        ------
        HarnessError
            If the subprocess is not running, there is no active session, or
            the request times out.
        """
        session_id = session_id or self._session_id
        if not session_id:
            raise HarnessError("no active ACP session")

        request = SessionPromptRequest(
            sessionId=session_id,
            prompt=[TextContentBlock(text=text)],
        )

        async with self._prompt_lock:
            async with self._chunk_lock:
                self._message_chunks.clear()
            async with self._update_lock:
                self._updates.clear()

            try:
                result = await self._send_request(
                    "session/prompt",
                    request.model_dump(exclude_none=True),
                    timeout=timeout,
                )
                SessionPromptResponse(**result)
            except asyncio.TimeoutError:
                logger.warning("session/prompt timed out for session %s", session_id)
                try:
                    await self._send_notification(
                        "session/cancel", {"sessionId": session_id}
                    )
                except Exception:
                    logger.debug("session/cancel after timeout failed", exc_info=True)
                raise HarnessError("session/prompt timed out")

        async with self._chunk_lock:
            full_text = "".join(self._message_chunks)

        return self._extract_result(full_text)

    async def send(
        self,
        task: str,
        context: Any,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> HarnessResult:
        """Send a structured prompt with ``task`` and ``context`` fields.

        This is a convenience wrapper around :meth:`prompt` that encodes the
        context as JSON. ``context`` may be a string, dict, or any JSON
        serializable value.
        """
        prompt_text = json.dumps({"task": task, "context": context}, ensure_ascii=False)
        return await self.prompt(prompt_text, session_id=session_id, timeout=timeout)

    async def steer(
        self,
        text: str,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> HarnessResult:
        """Inject mid-flight context without cancelling the current turn.

        This is an alias for :meth:`prompt` so handlers can express intent
        clearly. The ACP session is reused.
        """
        return await self.prompt(text, session_id=session_id, timeout=timeout)

    async def abort(self, session_id: str | None = None) -> None:
        """Send a ``session/cancel`` notification to stop the current turn.

        ``cancel`` is provided as an alias for callers who prefer that name.
        """
        session_id = session_id or self._session_id
        if not session_id:
            raise HarnessError("no active ACP session")
        await self._send_notification("session/cancel", {"sessionId": session_id})

    async def cancel(self, session_id: str | None = None) -> None:
        """Alias for :meth:`abort`."""
        await self.abort(session_id=session_id)

    async def close(self, session_id: str | None = None) -> None:
        """Send ``session/close`` for the given session and clear local state."""
        session_id = session_id or self._session_id
        if not session_id:
            raise HarnessError("no active ACP session")
        await self._send_request("session/close", {"sessionId": session_id})
        if session_id == self._session_id:
            self._session_id = None

    async def get_updates(self) -> list[dict[str, Any]]:
        """Return a snapshot of collected ``session/update`` notifications."""
        async with self._update_lock:
            return list(self._updates)

    async def clear_updates(self) -> None:
        """Clear the buffered notification list."""
        async with self._update_lock:
            self._updates.clear()

    # ---------------------------------------------------------------------=
    # Handshake helpers
    # ---------------------------------------------------------------------

    async def _initialize(self) -> None:
        request = InitializeRequest(protocolVersion=1)
        result = await self._send_request("initialize", request.model_dump())
        response = InitializeResponse(**result)
        if response.protocolVersion != 1:
            raise HarnessError(
                f"Unsupported ACP protocol version: {response.protocolVersion}"
            )
        self._agent_info = response.agentInfo

    async def _authenticate(self) -> None:
        request = AuthenticateRequest(method="agent")
        result = await self._send_request("authenticate", request.model_dump())
        response = AuthenticateResponse(**result)
        if not response.success:
            raise HarnessError(
                f"ACP authentication failed: {response.model_dump_json()}"
            )

    async def _session_new(self, cwd: str | None = None) -> str:
        request = SessionNewRequest(cwd=cwd or self._cwd, mcpServers=[])
        result = await self._send_request("session/new", request.model_dump())
        response = SessionNewResponse(**result)
        self._session_id = response.sessionId
        return response.sessionId

    # ---------------------------------------------------------------------=
    # Transport
    # ---------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: Any,
        timeout: float | None = None,
    ) -> Any:
        if self._process is None or self._process.stdin is None:
            raise HarnessError("harness process not running")
        if self._crashed:
            raise HarnessError("harness process has crashed")

        timeout = timeout or self._timeout

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future

        try:
            await self._send_raw(message)
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        finally:
            self._pending.pop(req_id, None)

    async def _send_notification(self, method: str, params: Any) -> None:
        if self._process is None or self._process.stdin is None:
            raise HarnessError("harness process not running")
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._send_raw(message)

    async def _send_raw(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise HarnessError("harness process not running")
        line = json.dumps(message) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    # ---------------------------------------------------------------------=
    # Read loop / dispatch
    # ---------------------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            try:
                if self._process is None or self._process.stdout is None:
                    break
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning("Ignoring non-JSON ACP line: %s", exc)
                    continue
                await self._dispatch(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in ACP read loop")
                continue

        # Stdout closed or process exited.
        self._crashed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(HarnessError("harness process exited unexpectedly"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            if "id" not in msg:
                await self._handle_notification(msg)
            else:
                await self._handle_server_request(msg)
            return

        if "id" not in msg:
            logger.warning("Ignoring ACP message without id or method: %s", msg)
            return

        req_id = msg["id"]
        future = self._pending.pop(req_id, None)
        if future is None:
            logger.warning("No pending request for ACP id %s", req_id)
            return

        if "error" in msg:
            try:
                error = ACPError(**msg["error"])
            except ValidationError:
                error = ACPError(code=-32603, message=str(msg["error"]))
            future.set_exception(HarnessError(f"ACP error {error.code}: {error.message}"))
        else:
            future.set_result(msg.get("result"))

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.debug(
                "harness stderr: %s", line.decode("utf-8", errors="replace").rstrip()
            )

    # ---------------------------------------------------------------------=
    # Agent → client handlers
    # ---------------------------------------------------------------------

    async def _handle_notification(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method != "session/update":
            logger.debug("Ignoring ACP notification: %s", method)
            return

        try:
            note = SessionUpdateNotification(**msg.get("params", {}))
        except ValidationError:
            logger.warning("Ignoring malformed session/update notification: %s", msg)
            return

        async with self._update_lock:
            self._updates.append(note.model_dump())

        if note.type == "agent_message_chunk":
            text = self._extract_message_text(note)
            if text:
                async with self._chunk_lock:
                    self._message_chunks.append(text)

    def _extract_message_text(self, note: SessionUpdateNotification) -> str | None:
        params = note.model_dump()
        content = params.get("text") or params.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return content.get("text", "")
        return None

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {})

        logger.debug("Handling agent → client request: %s id=%s", method, req_id)

        try:
            if method == "session/request_permission":
                request = PermissionRequest(**params)
                result = await self._handle_permission(request)
            elif method == "fs/read_text_file":
                request = FsReadRequest(**params)
                result = await self._handle_fs_read(request)
            elif method == "fs/write_text_file":
                request = FsWriteRequest(**params)
                result = await self._handle_fs_write(request)
            elif method == "terminal/create":
                request = TerminalCreateRequest(**params)
                result = await self._handle_terminal_create(request)
            elif method == "terminal/output":
                result = await self._handle_terminal_output(params)
            else:
                result = {
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    }
                }
        except Exception as exc:
            logger.exception("Error handling agent request %s", method)
            result = {"error": {"code": -32603, "message": str(exc)}}

        response: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if "error" in result:
            response["error"] = result["error"]
        else:
            response["result"] = result.get("result", {})
        await self._send_raw(response)

    async def _handle_permission(self, request: PermissionRequest) -> dict[str, Any]:
        allowed, reason = self._policy.allowed(request)
        return {"result": {"allowed": allowed, "reason": reason}}

    async def _handle_fs_read(self, request: FsReadRequest) -> dict[str, Any]:
        if not self._policy._is_within_workdir(request.path):
            return {
                "error": {
                    "code": -32000,
                    "message": f"Path {request.path} is outside the working directory",
                }
            }
        try:
            content = Path(request.path).read_text(encoding="utf-8")
            return {"result": {"content": content}}
        except Exception as exc:
            return {
                "error": {
                    "code": -32000,
                    "message": f"Failed to read file: {exc}",
                }
            }

    async def _handle_fs_write(self, request: FsWriteRequest) -> dict[str, Any]:
        return {
            "error": {
                "code": -32000,
                "message": "Write operations are declined by policy",
            }
        }

    async def _handle_terminal_create(
        self, request: TerminalCreateRequest
    ) -> dict[str, Any]:
        return {
            "error": {
                "code": -32000,
                "message": "Terminal execution is declined by policy",
            }
        }

    async def _handle_terminal_output(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "error": {
                "code": -32000,
                "message": "Terminal output is declined by policy",
            }
        }

    # ---------------------------------------------------------------------=
    # Result extraction
    # ---------------------------------------------------------------------

    def _extract_result(self, text: str) -> HarnessResult:
        """Parse the last fenced JSON block or fall back to the heuristic."""
        matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if matches:
            last_json = matches[-1]
            try:
                data = json.loads(last_json)
            except json.JSONDecodeError:
                return HarnessResult(
                    status="error",
                    payload={
                        "message": "Malformed fenced JSON block",
                        "raw": last_json,
                    },
                )

            if isinstance(data, dict) and "status" in data:
                status = data["status"]
                if status not in ("success", "needs_input", "error"):
                    status = "error"
                payload = data.get("payload", data)
                return HarnessResult(status=status, payload=payload)

            return HarnessResult(status="success", payload=data)

        if self._looks_like_question(text):
            return HarnessResult(status="needs_input", payload=text)

        return HarnessResult(status="success", payload=text)

    def _looks_like_question(self, text: str) -> bool:
        """Conservative heuristic for question-like responses."""
        stripped = text.strip()
        if stripped.endswith("?"):
            return True
        lower = stripped.lower()
        question_phrases = (
            "what is",
            "how do",
            "why is",
            "when is",
            "where is",
            "who is",
            "which",
            "can you",
            "could you",
            "would you",
            "please provide",
            "do you",
        )
        has_question_phrase = any(phrase in lower for phrase in question_phrases)
        return has_question_phrase and "?" in text


class HarnessPool:
    """Manage one ``HarnessBackend`` per persona and restart backends on failure.

    Parameters
    ----------
    config:
        Optional ``ShipplyConfig`` object. If provided, the pool reads the
        ``binary`` and ``args`` attributes from each persona config, falling
        back to the defaults below.
    binary:
        Default command to spawn. Default: ``"omp"``.
    args:
        Default arguments after ``binary``. Default: ``["acp"]``.
    cwd:
        Default working directory for backends. Default: current directory.
    timeout:
        Default request timeout in seconds. Default: ``300``.
    """

    def __init__(
        self,
        config: Any | None = None,
        binary: str = "omp",
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: float = 300.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._binary = binary
        self._args = list(args) if args else ["acp"]
        self._cwd = str(cwd or os.getcwd())
        self._timeout = timeout
        self._env = {**os.environ, **(env or {})}
        self._backends: dict[str, HarnessBackend] = {}
        self._lock = asyncio.Lock()

    def _persona_settings(self, persona: str) -> dict[str, Any]:
        """Build backend kwargs for a persona, merging config and defaults."""
        settings: dict[str, Any] = {
            "binary": self._binary,
            "args": list(self._args),
            "cwd": self._cwd,
            "timeout": self._timeout,
            "env": dict(self._env),
        }
        if self._config is not None:
            personas = getattr(self._config, "personas", {})
            persona_cfg = personas.get(persona)
            if persona_cfg is not None:
                settings["binary"] = getattr(persona_cfg, "binary", settings["binary"])
                settings["args"] = getattr(persona_cfg, "args", settings["args"])
                persona_env = getattr(persona_cfg, "env", None)
                if persona_env:
                    settings["env"].update(persona_env)
        return settings

    async def get(self, persona: str) -> HarnessBackend:
        """Return the backend for ``persona``, creating or restarting it."""
        async with self._lock:
            backend = self._backends.get(persona)
            if backend is not None and backend._crashed:
                logger.info("Restarting crashed harness for persona %s", persona)
                try:
                    await backend.shutdown()
                except Exception:
                    logger.debug("shutdown of crashed backend failed", exc_info=True)
                backend = None

            if backend is None:
                settings = self._persona_settings(persona)
                backend = HarnessBackend(**settings)
                await backend.start()
                self._backends[persona] = backend

            return backend

    async def restart(self, persona: str) -> HarnessBackend:
        """Explicitly shut down and recreate the backend for ``persona``."""
        async with self._lock:
            backend = self._backends.pop(persona, None)
            if backend is not None:
                try:
                    await backend.shutdown()
                except Exception:
                    logger.debug("shutdown during restart failed", exc_info=True)
        return await self.get(persona)

    async def shutdown_all(self) -> None:
        """Shut down every backend in the pool."""
        async with self._lock:
            backends = list(self._backends.values())
            self._backends.clear()
        for backend in backends:
            try:
                await backend.shutdown()
            except Exception:
                logger.debug("shutdown during pool shutdown failed", exc_info=True)
