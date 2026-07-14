"""Tests for the ACP harness backend and Pydantic ACP models."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shipply.protocols.acp import (
    ACPError,
    ACPNotification,
    ACPRequest,
    ACPResponse,
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
    TerminalOutputRequest,
    TextContentBlock,
)
from shipply.harness import HarnessBackend, HarnessError, HarnessPool, _PermissionPolicy


# ---------------------------------------------------------------------------
# ACP model round-trip and validation tests
# ---------------------------------------------------------------------------


def test_acp_request_round_trip():
    req = ACPRequest(id=1, method="initialize", params={"protocolVersion": 1})
    data = req.model_dump()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert data["method"] == "initialize"
    assert data["params"] == {"protocolVersion": 1}
    restored = ACPRequest(**data)
    assert restored.id == req.id
    assert restored.method == req.method


def test_acp_response_raises_for_error():
    resp = ACPResponse(id=1, error=ACPError(code=-32600, message="Invalid Request"))
    with pytest.raises(RuntimeError, match="ACP error -32600"):
        resp.raise_for_error()
    with pytest.raises(RuntimeError, match="Invalid Request"):
        resp.get_result()


def test_acp_response_get_result():
    resp = ACPResponse(id=1, result={"protocolVersion": 1})
    assert resp.get_result() == {"protocolVersion": 1}


def test_acp_notification_round_trip():
    note = ACPNotification(method="session/update", params={"type": "usage_update"})
    data = note.model_dump()
    assert "id" not in data
    restored = ACPNotification(**data)
    assert restored.method == "session/update"


def test_text_content_block_validation():
    block = TextContentBlock(text="hello")
    assert block.type == "text"
    assert block.text == "hello"
    with pytest.raises(ValidationError):
        TextContentBlock(type="text")
    with pytest.raises(ValidationError):
        TextContentBlock(type="image", text="x")


def test_initialize_models():
    req = InitializeRequest(protocolVersion=1)
    assert req.protocolVersion == 1

    resp = InitializeResponse(
        protocolVersion=1,
        agentInfo={"name": "mock"},
        authMethods=["agent"],
    )
    assert resp.model_dump()["authMethods"] == ["agent"]

    with pytest.raises(ValidationError):
        InitializeResponse(agentInfo={})


def test_authenticate_models():
    req = AuthenticateRequest(method="agent")
    assert req.method == "agent"
    assert req.options == {}

    resp = AuthenticateResponse(success=True, method="agent")
    assert resp.success is True


def test_session_models():
    req = SessionNewRequest(cwd="/tmp", mcpServers=[])
    assert req.cwd == "/tmp"

    resp = SessionNewResponse(sessionId="sess-1")
    assert resp.sessionId == "sess-1"

    req2 = SessionPromptRequest(sessionId="sess-1", prompt=[TextContentBlock(text="hi")])
    data = req2.model_dump()
    assert data["prompt"] == [{"type": "text", "text": "hi"}]

    resp2 = SessionPromptResponse(stopReason="end_turn")
    assert resp2.stopReason == "end_turn"


def test_session_update_notification():
    note = SessionUpdateNotification(sessionId="sess-1", type="agent_message_chunk")
    assert note.model_dump()["type"] == "agent_message_chunk"
    with pytest.raises(ValidationError):
        SessionUpdateNotification(type="unknown")


def test_permission_request_model():
    req = PermissionRequest(
        sessionId="sess-1",
        tool_name="fs/read_text_file",
        arguments={"path": "/tmp/file.txt"},
    )
    assert req.tool_name == "fs/read_text_file"
    assert req.arguments["path"] == "/tmp/file.txt"


def test_fs_and_terminal_models():
    read_req = FsReadRequest(sessionId="sess-1", path="/tmp/file.txt")
    assert read_req.path == "/tmp/file.txt"

    write_req = FsWriteRequest(sessionId="sess-1", path="/tmp/file.txt", content="x")
    assert write_req.content == "x"

    term_req = TerminalCreateRequest(sessionId="sess-1", command="ls", cwd="/tmp")
    assert term_req.command == "ls"

    term_out = TerminalOutputRequest(sessionId="sess-1", terminal_id="t1", output="out")
    assert term_out.output == "out"


def test_harness_result_validation():
    result = HarnessResult(status="success", payload={"foo": 1})
    assert result.status == "success"
    assert result.payload == {"foo": 1}

    with pytest.raises(ValidationError):
        HarnessResult(status="invalid", payload="x")


# ---------------------------------------------------------------------------
# Mock ACP agent subprocess
# ---------------------------------------------------------------------------

MOCK_AGENT_SCRIPT = r'''
import json
import sys
import time

LOG_PATH = sys.argv[1]
SCENARIO_PATH = sys.argv[2]
STATE_PATH = sys.argv[3]


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def log(entry):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def send(obj):
    line = json.dumps(obj)
    print(line, flush=True)
    log({"direction": "out", "msg": obj})


def read_line():
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        msg = line.strip()
    log({"direction": "in", "msg": msg})
    return line


def main():
    with open(SCENARIO_PATH, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    state = load_state()
    session_id = scenario.get("session_id", "mock-session")
    session_new_count = state.get("session_new_count", 0)
    prompt_count = state.get("prompt_count", 0)

    while True:
        line = read_line()
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            result = scenario.get(
                "initialize",
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "mock"},
                    "agentCapabilities": {},
                    "authMethods": ["agent"],
                },
            )
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "authenticate":
            result = scenario.get("authenticate", {"success": True})
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "session/new":
            session_new_count += 1
            save_state({"session_new_count": session_new_count, "prompt_count": prompt_count})
            result = scenario.get(
                f"session_new_{session_new_count}",
                scenario.get("session_new", {"sessionId": session_id, "configOptions": {}}),
            )
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "session/prompt":
            prompt_count += 1
            save_state({"session_new_count": session_new_count, "prompt_count": prompt_count})
            pr = scenario.get(
                f"prompt_{prompt_count}",
                scenario.get("prompt_response", {}),
            )
            for note in pr.get("notifications", []):
                send({"jsonrpc": "2.0", "method": "session/update", "params": note})
            for sreq in pr.get("server_requests", []):
                sreq_id = sreq.get("id", 100 + prompt_count)
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": sreq["method"],
                        "id": sreq_id,
                        "params": sreq.get("params", {}),
                    }
                )
                read_line()  # consume the harness response
            if pr.get("crash"):
                sys.exit(1)
            if pr.get("no_response"):
                sleep = pr.get("sleep", 10)
                time.sleep(sleep)
                continue
            result = pr.get("result", {"stopReason": "end_turn"})
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "session/close":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "session/cancel":
            pass
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def mock_agent(tmp_path):
    """Factory for a mock ACP agent script and its log/scenario files."""

    def _make(scenario: dict[str, Any]) -> tuple[Path, Path, Path, list[str]]:
        log_path = tmp_path / "acp.log"
        scenario_path = tmp_path / "scenario.json"
        state_path = tmp_path / "acp_state.json"
        script_path = tmp_path / "mock_acp_agent.py"
        script_path.write_text(MOCK_AGENT_SCRIPT, encoding="utf-8")
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        state_path.write_text("{}", encoding="utf-8")
        return script_path, log_path, scenario_path, [str(script_path), str(log_path), str(scenario_path), str(state_path)]

    return _make


def _read_log(log_path: Path) -> list[dict[str, Any]]:
    text = log_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def _requests_in(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry["msg"]
        for entry in log
        if entry["direction"] == "in" and isinstance(entry["msg"], dict) and "method" in entry["msg"]
    ]


def _responses_out(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry["msg"]
        for entry in log
        if entry["direction"] == "out" and isinstance(entry["msg"], dict) and "id" in entry["msg"]
    ]


def _responses_in(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return messages sent by the harness back to the mock agent (id + result/error)."""
    return [
        entry["msg"]
        for entry in log
        if entry["direction"] == "in"
        and isinstance(entry["msg"], dict)
        and "id" in entry["msg"]
        and ("result" in entry["msg"] or "error" in entry["msg"])
    ]


# ---------------------------------------------------------------------------
# Harness lifecycle tests
# ---------------------------------------------------------------------------


async def test_harness_lifecycle_start_initialize_auth_session_new(mock_agent, tmp_path):
    scenario = {"session_id": "sess-1"}
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        assert backend._session_id == "sess-1"
        assert backend._agent_info == {"name": "mock"}
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    methods = [r["method"] for r in _requests_in(log)]
    assert methods[:3] == ["initialize", "authenticate", "session/new"]

    # initialize content
    init_req = [r for r in _requests_in(log) if r["method"] == "initialize"][0]
    assert init_req["params"] == {"protocolVersion": 1}

    # authenticate content
    auth_req = [r for r in _requests_in(log) if r["method"] == "authenticate"][0]
    assert auth_req["params"]["method"] == "agent"

    # session/new content
    session_req = [r for r in _requests_in(log) if r["method"] == "session/new"][0]
    assert session_req["params"]["cwd"] == str(tmp_path)
    assert session_req["params"]["mcpServers"] == []


async def test_prompt_sends_session_prompt_request_with_content_blocks(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "ok"},
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        result = await backend.prompt("hello world", timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    prompt_reqs = [r for r in _requests_in(log) if r["method"] == "session/prompt"]
    assert len(prompt_reqs) == 1
    prompt_req = prompt_reqs[0]
    assert prompt_req["params"]["sessionId"] == "sess-1"
    assert prompt_req["params"]["prompt"] == [{"type": "text", "text": "hello world"}]
    assert result.status == "success"


async def test_session_update_notifications_collected(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_thought_chunk", "text": "thinking"},
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "Hello"},
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": " world"},
                {"sessionId": "sess-1", "type": "tool_call", "tool_call": {"name": "read"}},
                {"sessionId": "sess-1", "type": "usage_update", "usage": {"tokens": 42}},
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("go", timeout=5.0)
        updates = await backend.get_updates()
    finally:
        await backend.shutdown()

    types = [u["type"] for u in updates]
    assert types == [
        "agent_thought_chunk",
        "agent_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "usage_update",
    ]
    assert "".join(u.get("text", "") for u in updates if u["type"] == "agent_message_chunk") == "Hello world"


async def test_send_structured_prompt(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "done"},
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        result = await backend.send("brainstorm", {"topic": "x"}, timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    prompt_reqs = [r for r in _requests_in(log) if r["method"] == "session/prompt"]
    assert len(prompt_reqs) == 1
    prompt_text = prompt_reqs[0]["params"]["prompt"][0]["text"]
    data = json.loads(prompt_text)
    assert data["task"] == "brainstorm"
    assert data["context"] == {"topic": "x"}
    assert result.status == "success"


async def test_steer_and_cancel_lifecycle(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "steered"},
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        result = await backend.steer("extra context", timeout=5.0)
        assert result.status == "success"
        await backend.cancel()
    finally:
        await backend.shutdown()


async def test_new_session_and_close(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "session_new_1": {"sessionId": "sess-1", "configOptions": {}},
        "session_new_2": {"sessionId": "sess-2", "configOptions": {}},
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        assert backend._session_id == "sess-1"
        new_id = await backend.new_session()
        assert new_id == "sess-2"
        assert backend._session_id == "sess-2"
        await backend.close()
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    methods = [r["method"] for r in _requests_in(log)]
    assert methods.count("session/new") == 2
    assert "session/close" in methods


# ---------------------------------------------------------------------------
# Agent -> client handler tests
# ---------------------------------------------------------------------------


async def test_permission_request_returns_allowed_and_reason(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "server_requests": [
                {
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "sess-1",
                        "tool_name": "fs/read_text_file",
                        "description": "read a file",
                        "arguments": {"path": str(tmp_path / "file.txt")},
                    },
                }
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("read", timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    responses = _responses_in(log)
    perm_responses = [r for r in responses if "allowed" in r.get("result", {})]
    assert len(perm_responses) == 1
    assert perm_responses[0]["result"]["allowed"] is True
    assert perm_responses[0]["result"]["reason"] is None


async def test_fs_read_text_file_inside_workdir(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "server_requests": [
                {
                    "method": "fs/read_text_file",
                    "params": {"sessionId": "sess-1", "path": str(tmp_path / "file.txt")},
                }
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    (tmp_path / "file.txt").write_text("hello from file", encoding="utf-8")
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("read", timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    responses = _responses_in(log)
    read_responses = [r for r in responses if "content" in r.get("result", {})]
    assert len(read_responses) == 1
    assert read_responses[0]["result"]["content"] == "hello from file"


async def test_fs_read_text_file_outside_workdir(mock_agent, tmp_path):
    outside_dir = Path(tempfile.mkdtemp(prefix="shipply-outside-"))
    outside = outside_dir / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "server_requests": [
                {
                    "method": "fs/read_text_file",
                    "params": {"sessionId": "sess-1", "path": str(outside)},
                }
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("read outside", timeout=5.0)
    finally:
        await backend.shutdown()
        shutil.rmtree(outside_dir, ignore_errors=True)

    log = _read_log(log_path)
    responses = _responses_in(log)
    errors = [r for r in responses if "error" in r and "outside the working directory" in r["error"].get("message", "")]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == -32000


async def test_fs_write_text_file_declined(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "server_requests": [
                {
                    "method": "fs/write_text_file",
                    "params": {
                        "sessionId": "sess-1",
                        "path": str(tmp_path / "file.txt"),
                        "content": "overwrite",
                    },
                }
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("write", timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    responses = _responses_in(log)
    errors = [r for r in responses if "error" in r and "Write operations" in r["error"].get("message", "")]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == -32000


async def test_terminal_create_declined(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "server_requests": [
                {
                    "method": "terminal/create",
                    "params": {"sessionId": "sess-1", "command": "ls"},
                }
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        await backend.prompt("terminal", timeout=5.0)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    responses = _responses_in(log)
    errors = [r for r in responses if "error" in r and "Terminal execution" in r["error"].get("message", "")]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == -32000


# ---------------------------------------------------------------------------
# Permission policy unit tests
# ---------------------------------------------------------------------------


def test_permission_policy_allows_reads_inside_workdir(tmp_path):
    policy = _PermissionPolicy(tmp_path)
    req = PermissionRequest(
        sessionId="s1",
        tool_name="fs/read_text_file",
        arguments={"path": str(tmp_path / "file.txt")},
    )
    allowed, reason = policy.allowed(req)
    assert allowed is True
    assert reason is None


def test_permission_policy_declines_reads_outside_workdir(tmp_path):
    policy = _PermissionPolicy(tmp_path)
    req = PermissionRequest(
        sessionId="s1",
        tool_name="fs/read_text_file",
        arguments={"path": "/etc/passwd"},
    )
    allowed, reason = policy.allowed(req)
    assert allowed is False
    assert "outside the working directory" in (reason or "")


def test_permission_policy_declines_write_and_terminal(tmp_path):
    policy = _PermissionPolicy(tmp_path)
    write_req = PermissionRequest(
        sessionId="s1",
        tool_name="fs/write_text_file",
        arguments={"path": str(tmp_path / "file.txt")},
    )
    allowed, reason = policy.allowed(write_req)
    assert allowed is False
    assert "write operations" in (reason or "").lower()

    term_req = PermissionRequest(sessionId="s1", tool_name="terminal/create")
    allowed, reason = policy.allowed(term_req)
    assert allowed is False
    assert "terminal" in (reason or "").lower()


def test_permission_policy_declines_unknown(tmp_path):
    policy = _PermissionPolicy(tmp_path)
    req = PermissionRequest(sessionId="s1", tool_name="some_unknown_tool")
    allowed, reason = policy.allowed(req)
    assert allowed is False
    assert "not allowed" in (reason or "").lower()


# ---------------------------------------------------------------------------
# Fenced-JSON result extraction tests
# ---------------------------------------------------------------------------


@pytest.fixture
def extractor(tmp_path):
    return HarnessBackend(cwd=str(tmp_path))


def test_extract_fenced_json_returns_status_and_payload(extractor):
    text = "Explanation\n```json\n{\"status\": \"success\", \"payload\": {\"answer\": 42}}\n```"
    result = extractor._extract_result(text)
    assert result.status == "success"
    assert result.payload == {"answer": 42}


def test_extract_fenced_needs_input_status(extractor):
    text = "```json\n{\"status\": \"needs_input\", \"payload\": \"missing topic\"}\n```"
    result = extractor._extract_result(text)
    assert result.status == "needs_input"
    assert result.payload == "missing topic"


def test_extract_fenced_error_status(extractor):
    text = "```json\n{\"status\": \"error\", \"payload\": \"bad thing\"}\n```"
    result = extractor._extract_result(text)
    assert result.status == "error"
    assert result.payload == "bad thing"


def test_extract_fenced_invalid_status_becomes_error(extractor):
    text = "```json\n{\"status\": \"weird\", \"payload\": \"x\"}\n```"
    result = extractor._extract_result(text)
    assert result.status == "error"
    assert result.payload == "x"


def test_extract_fenced_non_dict_payload_is_success(extractor):
    text = "```json\n[1, 2, 3]\n```"
    result = extractor._extract_result(text)
    assert result.status == "success"
    assert result.payload == [1, 2, 3]


def test_extract_malformed_fenced_json_returns_error(extractor):
    text = "Some text\n```json\n{not valid json\n```"
    result = extractor._extract_result(text)
    assert result.status == "error"
    assert result.payload["message"] == "Malformed fenced JSON block"
    assert result.payload["raw"] == "{not valid json"


def test_extract_no_fence_question_like_returns_needs_input(extractor):
    text = "What is the deadline for this task?"
    result = extractor._extract_result(text)
    assert result.status == "needs_input"
    assert result.payload == text


def test_extract_no_fence_question_phrase_with_question_mark(extractor):
    text = "Could you please provide more details?"
    result = extractor._extract_result(text)
    assert result.status == "needs_input"
    assert result.payload == text


def test_extract_no_fence_statement_returns_success(extractor):
    text = "The task is complete and ready for review."
    result = extractor._extract_result(text)
    assert result.status == "success"
    assert result.payload == text


def test_extract_no_fence_non_question_with_question_mark_only(extractor):
    text = "This is a statement with a ? in the middle."
    result = extractor._extract_result(text)
    # Has a question mark but no question phrase, so it should be success
    assert result.status == "success"
    assert result.payload == text


def test_extract_uses_last_fenced_block(extractor):
    text = (
        "```json\n{\"status\": \"success\", \"payload\": 1}\n```"
        "more text\n"
        "```json\n{\"status\": \"error\", \"payload\": \"latest\"}\n```"
    )
    result = extractor._extract_result(text)
    assert result.status == "error"
    assert result.payload == "latest"


# ---------------------------------------------------------------------------
# HarnessPool tests
# ---------------------------------------------------------------------------


async def test_harness_pool_reuses_backend(mock_agent, tmp_path):
    scenario = {"session_id": "sess-1"}
    script, _, _, args = mock_agent(scenario)
    pool = HarnessPool(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        b1 = await pool.get("scout")
        b2 = await pool.get("scout")
        assert b1 is b2
        assert b1._process is not None
    finally:
        await pool.shutdown_all()


async def test_harness_pool_returns_distinct_backend_per_persona(mock_agent, tmp_path):
    scenario = {"session_id": "sess-1"}
    script, _, _, args = mock_agent(scenario)
    pool = HarnessPool(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        b1 = await pool.get("scout")
        b2 = await pool.get("doc-review")
        assert b1 is not b2
    finally:
        await pool.shutdown_all()


async def test_harness_pool_restarts_crashed_backend(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {"crash": True},
    }
    script, _, _, args = mock_agent(scenario)
    pool = HarnessPool(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        b1 = await pool.get("scout")
        with pytest.raises(HarnessError):
            await b1.prompt("crash", timeout=5.0)
        assert b1._crashed is True
        b2 = await pool.get("scout")
        assert b2 is not b1
        assert b2._process is not None
        assert b2._crashed is False
    finally:
        await pool.shutdown_all()


async def test_harness_pool_restart_replaces_backend(mock_agent, tmp_path):
    scenario = {"session_id": "sess-1"}
    script, _, _, args = mock_agent(scenario)
    pool = HarnessPool(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        b1 = await pool.get("scout")
        b2 = await pool.restart("scout")
        assert b2 is not b1
    finally:
        await pool.shutdown_all()


# ---------------------------------------------------------------------------
# Timeout and crash recovery tests
# ---------------------------------------------------------------------------


async def test_prompt_timeout_sends_cancel_and_raises(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {"no_response": True, "sleep": 2},
    }
    script, log_path, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        with pytest.raises(HarnessError, match="timed out"):
            await backend.prompt("timeout", timeout=0.2)
    finally:
        await backend.shutdown()

    log = _read_log(log_path)
    methods = [r["method"] for r in _requests_in(log)]
    assert "session/cancel" in methods


async def test_crash_mid_request_raises_harness_error(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_response": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "partial"},
            ],
            "crash": True,
        },
    }
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        with pytest.raises(HarnessError):
            await backend.prompt("crash", timeout=5.0)
        assert backend._crashed is True
    finally:
        await backend.shutdown()


async def test_harness_restart_recovers_after_crash(mock_agent, tmp_path):
    scenario = {
        "session_id": "sess-1",
        "prompt_1": {"crash": True},
        "prompt_2": {
            "notifications": [
                {"sessionId": "sess-1", "type": "agent_message_chunk", "text": "recovered"},
            ],
            "result": {"stopReason": "end_turn"},
        },
    }
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    try:
        await backend.start()
        with pytest.raises(HarnessError):
            await backend.prompt("crash", timeout=5.0)
        await backend.restart()
        result = await backend.prompt("again", timeout=5.0)
        assert result.status == "success"
    finally:
        await backend.shutdown()


async def test_start_without_omp_binary_raises_harness_error(tmp_path):
    with pytest.raises(HarnessError, match="Failed to start harness process"):
        backend = HarnessBackend(
            binary="/definitely/not/omp",
            args=["acp"],
            cwd=str(tmp_path),
            timeout=2.0,
        )
        await backend.start()


async def test_unsupported_protocol_version_raises(mock_agent, tmp_path):
    scenario = {"initialize": {"protocolVersion": 2, "agentInfo": {}}}
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    with pytest.raises(HarnessError, match="Unsupported ACP protocol version"):
        await backend.start()
    await backend.shutdown()


async def test_authentication_failure_raises(mock_agent, tmp_path):
    scenario = {"authenticate": {"success": False}}
    script, _, _, args = mock_agent(scenario)
    backend = HarnessBackend(
        binary=sys.executable,
        args=args,
        cwd=str(tmp_path),
        timeout=5.0,
    )
    with pytest.raises(HarnessError, match="ACP authentication failed"):
        await backend.start()
    await backend.shutdown()
