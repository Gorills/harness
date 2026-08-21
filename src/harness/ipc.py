from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
_REQUEST_ID_MAX_LENGTH = 64
_DEFAULT_TIMEOUT_SECONDS = 2.0


class IpcError(RuntimeError):
    """Base class for local Harness IPC failures."""


class IpcProtocolError(IpcError):
    """Raised when an IPC peer violates the bounded Harness wire contract."""


class IpcMessageTooLargeError(IpcProtocolError):
    """Raised when one IPC message exceeds the configured hard byte limit."""


class IpcTransportError(IpcError):
    """Raised when the local transport cannot connect or complete a request."""


class UnsupportedIpcTransportError(IpcTransportError):
    """Raised when this bounded implementation has no proven transport for the platform."""


class IpcRemoteError(IpcError):
    """Raised when the daemon returns a structured IPC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Bounded result returned by the first read-only daemon status request."""

    schema_version: int
    project_count: int
    workspace_count: int


@dataclass(frozen=True, slots=True)
class IpcRequest:
    """Validated internal request independent of MCP wire objects."""

    request_id: str
    method: str


def request_status(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> StatusResult:
    """Request daemon status over the current POSIX local IPC transport."""
    _require_posix_transport()
    request_id = uuid4().hex
    payload = _encode_json(
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "status"}
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(payload)
            response = _decode_json(_receive_frame(client))
    except TimeoutError as exc:
        raise IpcTransportError("local IPC request timed out") from exc
    except OSError as exc:
        raise IpcTransportError(f"local IPC transport failed: {exc}") from exc

    return _status_from_response(response, expected_request_id=request_id)


def receive_request(peer: socket.socket) -> IpcRequest:
    """Receive and validate exactly one bounded request frame."""
    payload = _decode_json(_receive_frame(peer))
    if set(payload) != {"version", "request_id", "method"}:
        raise IpcProtocolError("request fields do not match the IPC schema")

    version = payload["version"]
    request_id = payload["request_id"]
    method = payload["method"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise IpcProtocolError("request version must be an integer")
    if version != PROTOCOL_VERSION:
        raise IpcProtocolError(f"unsupported IPC protocol version: {version}")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > _REQUEST_ID_MAX_LENGTH
    ):
        raise IpcProtocolError("request_id must be a non-empty bounded string")
    if not isinstance(method, str) or method != "status":
        raise IpcProtocolError("unsupported IPC method")
    return IpcRequest(request_id=request_id, method=method)


def send_status_response(peer: socket.socket, request_id: str, status: StatusResult) -> None:
    """Send the exact success contract for the status path."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": status.schema_version,
                    "project_count": status.project_count,
                    "workspace_count": status.workspace_count,
                },
            }
        )
    )


def send_error_response(
    peer: socket.socket,
    *,
    request_id: str | None,
    code: str,
    message: str,
) -> None:
    """Send a bounded stable error without leaking source/context data."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )
    )


def _receive_frame(peer: socket.socket) -> bytes:
    data = bytearray()
    while True:
        chunk = peer.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            raise IpcProtocolError("IPC peer closed before terminating the message")
        newline_index = chunk.find(b"\n")
        if newline_index >= 0:
            data.extend(chunk[:newline_index])
            if len(data) > MAX_MESSAGE_BYTES:
                raise IpcMessageTooLargeError("IPC message exceeds the byte limit")
            if chunk[newline_index + 1 :]:
                raise IpcProtocolError("multiple IPC messages per connection are not supported")
            return bytes(data)
        data.extend(chunk)
        if len(data) > MAX_MESSAGE_BYTES:
            raise IpcMessageTooLargeError("IPC message exceeds the byte limit")


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcProtocolError("IPC message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise IpcProtocolError("IPC message must be a JSON object")
    return cast(dict[str, Any], value)


def _encode_json(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IpcMessageTooLargeError("IPC response exceeds the byte limit")
    return payload + b"\n"


def _status_from_response(response: dict[str, Any], *, expected_request_id: str) -> StatusResult:
    version = response.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise IpcProtocolError("daemon response uses an unsupported IPC protocol version")
    if response.get("request_id") != expected_request_id:
        raise IpcProtocolError("daemon response request_id does not match the request")
    ok = response.get("ok")
    if ok is False:
        if set(response) != {"version", "request_id", "ok", "error"}:
            raise IpcProtocolError("daemon error fields do not match the IPC schema")
        error = response["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise IpcProtocolError("daemon error payload does not match the IPC schema")
        code = error["code"]
        message = error["message"]
        if not isinstance(code, str) or not isinstance(message, str):
            raise IpcProtocolError("daemon error payload has invalid field types")
        raise IpcRemoteError(code, message)
    if ok is not True or set(response) != {"version", "request_id", "ok", "result"}:
        raise IpcProtocolError("daemon response fields do not match the IPC schema")

    result = response["result"]
    if not isinstance(result, dict) or set(result) != {
        "schema_version",
        "project_count",
        "workspace_count",
    }:
        raise IpcProtocolError("daemon status result does not match the IPC schema")
    values = (result["schema_version"], result["project_count"], result["workspace_count"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise IpcProtocolError("daemon status result has invalid field types")
    return StatusResult(
        schema_version=result["schema_version"],
        project_count=result["project_count"],
        workspace_count=result["workspace_count"],
    )


def _require_posix_transport() -> None:
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise UnsupportedIpcTransportError(
            "the Windows local-user IPC transport is not implemented in this bounded slice"
        )
