from __future__ import annotations

import json
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
_REQUEST_ID_MAX_LENGTH = 64
_HINT_SOURCE_MAX_LENGTH = 64
_HINT_PATH_MAX_LENGTH = 4096
_MAX_WORKSPACE_HINTS = 4
_DEFAULT_TIMEOUT_SECONDS = 2.0
_WORKSPACE_SCAN_TIMEOUT_SECONDS = 35.0


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
    """Bounded result returned by the global read-only daemon status request."""

    schema_version: int
    project_count: int
    workspace_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceStatusResult:
    """Bounded Workspace-scoped status returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    visibility_mode: str
    workspace_root: Path
    head: str | None
    branch: str | None
    dirty_path_count: int
    indexed_file_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceScanResult:
    """Bounded result returned after daemon-owned Workspace registration/index reconciliation."""

    schema_version: int
    workspace_id: str
    project_id: str
    workspace_root: Path
    file_count: int
    added: int
    updated: int
    removed: int


@dataclass(frozen=True, slots=True)
class IpcRequest:
    """Validated internal request independent of MCP wire objects."""

    request_id: str
    method: str
    workspace_hints: tuple[WorkspaceHint, ...] = ()
    workspace_path: Path | None = None


def request_status(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> StatusResult:
    """Request global daemon status over the current POSIX local IPC transport."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "status"},
        timeout=timeout,
    )
    return _status_from_response(response, expected_request_id=request_id)


def request_workspace_status(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> WorkspaceStatusResult:
    """Request one Workspace-scoped status using ordered absolute filesystem hints."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_status",
            "params": {"hints": _workspace_hints_to_wire(hints)},
        },
        timeout=timeout,
    )
    return _workspace_status_from_response(response, expected_request_id=request_id)


def request_workspace_scan(
    socket_path: Path,
    workspace_path: Path,
    *,
    timeout: float = _WORKSPACE_SCAN_TIMEOUT_SECONDS,
) -> WorkspaceScanResult:
    """Register/reconcile one Git Workspace through the daemon's bounded scan path."""
    request_id = uuid4().hex
    path = _workspace_path_to_wire(workspace_path)
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_scan",
            "params": {"path": path},
        },
        timeout=timeout,
    )
    return _workspace_scan_from_response(response, expected_request_id=request_id)


def receive_request(peer: socket.socket) -> IpcRequest:
    """Receive and validate exactly one bounded request frame."""
    payload = _decode_json(_receive_frame(peer))
    version = payload.get("version")
    request_id = payload.get("request_id")
    method = payload.get("method")
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
    if not isinstance(method, str):
        raise IpcProtocolError("request method must be a string")

    if method == "status":
        if set(payload) != {"version", "request_id", "method"}:
            raise IpcProtocolError("status request fields do not match the IPC schema")
        return IpcRequest(request_id=request_id, method=method)

    if method == "workspace_status":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace status request fields do not match the IPC schema")
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=_workspace_hints_from_params(payload["params"]),
        )

    if method == "workspace_scan":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace scan request fields do not match the IPC schema")
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_path=_workspace_path_from_params(payload["params"]),
        )

    raise IpcProtocolError("unsupported IPC method")


def send_status_response(peer: socket.socket, request_id: str, status: StatusResult) -> None:
    """Send the exact success contract for the global status path."""
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


def send_workspace_status_response(
    peer: socket.socket,
    request_id: str,
    status: WorkspaceStatusResult,
) -> None:
    """Send the exact success contract for one Workspace-scoped status request."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": status.schema_version,
                    "workspace_id": status.workspace_id,
                    "project_id": status.project_id,
                    "visibility_mode": status.visibility_mode,
                    "workspace_root": str(status.workspace_root),
                    "head": status.head,
                    "branch": status.branch,
                    "dirty_path_count": status.dirty_path_count,
                    "indexed_file_count": status.indexed_file_count,
                },
            }
        )
    )


def send_workspace_scan_response(
    peer: socket.socket,
    request_id: str,
    result: WorkspaceScanResult,
) -> None:
    """Send the exact success contract for one Workspace scan request."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "workspace_root": str(result.workspace_root),
                    "file_count": result.file_count,
                    "added": result.added,
                    "updated": result.updated,
                    "removed": result.removed,
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


def _request_response(
    socket_path: Path,
    request: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    _require_posix_transport()
    payload = _encode_json(request)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(payload)
            return _decode_json(_receive_frame(client))
    except TimeoutError as exc:
        raise IpcTransportError("local IPC request timed out") from exc
    except OSError as exc:
        raise IpcTransportError(f"local IPC transport failed: {exc}") from exc


def _workspace_hints_to_wire(hints: Sequence[WorkspaceHint]) -> list[dict[str, str]]:
    if not 1 <= len(hints) <= _MAX_WORKSPACE_HINTS:
        raise IpcProtocolError("workspace status requires between 1 and 4 hints")
    wire_hints: list[dict[str, str]] = []
    for hint in hints:
        path = str(hint.path)
        _validate_hint_fields(path, hint.source, hint.match_mode)
        wire_hints.append(
            {
                "path": path,
                "source": hint.source,
                "match_mode": hint.match_mode.value,
            }
        )
    return wire_hints


def _workspace_hints_from_params(value: object) -> tuple[WorkspaceHint, ...]:
    if not isinstance(value, dict) or set(value) != {"hints"}:
        raise IpcProtocolError("workspace status params do not match the IPC schema")
    raw_hints = value["hints"]
    if not isinstance(raw_hints, list) or not 1 <= len(raw_hints) <= _MAX_WORKSPACE_HINTS:
        raise IpcProtocolError("workspace status requires between 1 and 4 hints")

    hints: list[WorkspaceHint] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict) or set(raw_hint) != {
            "path",
            "source",
            "match_mode",
        }:
            raise IpcProtocolError("workspace hint fields do not match the IPC schema")
        path = raw_hint["path"]
        source = raw_hint["source"]
        raw_mode = raw_hint["match_mode"]
        if (
            not isinstance(path, str)
            or not isinstance(source, str)
            or not isinstance(raw_mode, str)
        ):
            raise IpcProtocolError("workspace hint fields have invalid types")
        try:
            match_mode = WorkspaceHintMatchMode(raw_mode)
        except ValueError as exc:
            raise IpcProtocolError("workspace hint uses an unsupported match mode") from exc
        _validate_hint_fields(path, source, match_mode)
        hints.append(WorkspaceHint(path=Path(path), source=source, match_mode=match_mode))
    return tuple(hints)


def _workspace_path_to_wire(path: Path) -> str:
    value = str(path)
    _validate_workspace_path(value)
    return value


def _workspace_path_from_params(value: object) -> Path:
    if not isinstance(value, dict) or set(value) != {"path"}:
        raise IpcProtocolError("workspace scan params do not match the IPC schema")
    raw_path = value["path"]
    if not isinstance(raw_path, str):
        raise IpcProtocolError("workspace scan path has an invalid type")
    _validate_workspace_path(raw_path)
    return Path(raw_path)


def _validate_workspace_path(path: str) -> None:
    if not path or len(path) > _HINT_PATH_MAX_LENGTH or "\x00" in path:
        raise IpcProtocolError("workspace scan path must be a non-empty bounded path")
    if not Path(path).is_absolute():
        raise IpcProtocolError("workspace scan path must be absolute")


def _validate_hint_fields(
    path: str,
    source: str,
    match_mode: WorkspaceHintMatchMode,
) -> None:
    if not path or len(path) > _HINT_PATH_MAX_LENGTH or "\x00" in path:
        raise IpcProtocolError("workspace hint path must be a non-empty bounded path")
    if not Path(path).is_absolute():
        raise IpcProtocolError("workspace hint path must be absolute")
    if not source or len(source) > _HINT_SOURCE_MAX_LENGTH or "\x00" in source:
        raise IpcProtocolError("workspace hint source must be a non-empty bounded string")
    if not isinstance(match_mode, WorkspaceHintMatchMode):
        raise IpcProtocolError("workspace hint uses an unsupported match mode")


def _receive_frame(peer: socket.socket) -> bytes:
    data = bytearray()
    original_timeout = peer.gettimeout()
    deadline = (
        monotonic() + original_timeout
        if original_timeout is not None and original_timeout > 0
        else None
    )
    try:
        while True:
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("IPC receive deadline exceeded")
                peer.settimeout(remaining)

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
    finally:
        if deadline is not None:
            peer.settimeout(original_timeout)


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
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"schema_version", "project_count", "workspace_count"}:
        raise IpcProtocolError("daemon status result does not match the IPC schema")
    values = (result["schema_version"], result["project_count"], result["workspace_count"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise IpcProtocolError("daemon status result has invalid field types")
    return StatusResult(
        schema_version=result["schema_version"],
        project_count=result["project_count"],
        workspace_count=result["workspace_count"],
    )


def _workspace_status_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceStatusResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "visibility_mode",
        "workspace_root",
        "head",
        "branch",
        "dirty_path_count",
        "indexed_file_count",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace status result does not match the IPC schema")

    schema_version = result["schema_version"]
    dirty_path_count = result["dirty_path_count"]
    indexed_file_count = result["indexed_file_count"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (schema_version, dirty_path_count, indexed_file_count)
    ):
        raise IpcProtocolError("daemon workspace status counts have invalid field types")

    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    visibility_mode = _bounded_response_string(result["visibility_mode"], "visibility_mode", 16)
    if visibility_mode not in {"normal", "hidden"}:
        raise IpcProtocolError("daemon workspace status has unsupported visibility mode")
    workspace_root_value = _bounded_response_string(
        result["workspace_root"],
        "workspace_root",
        _HINT_PATH_MAX_LENGTH,
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace status root must be absolute")

    head = result["head"]
    if head is not None:
        head = _bounded_response_string(head, "head", 64)
        if len(head) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in head
        ):
            raise IpcProtocolError("daemon workspace status has invalid HEAD identity")
    branch = result["branch"]
    if branch is not None:
        branch = _bounded_response_string(branch, "branch", MAX_MESSAGE_BYTES)

    return WorkspaceStatusResult(
        schema_version=schema_version,
        workspace_id=workspace_id,
        project_id=project_id,
        visibility_mode=visibility_mode,
        workspace_root=workspace_root,
        head=head,
        branch=branch,
        dirty_path_count=dirty_path_count,
        indexed_file_count=indexed_file_count,
    )


def _workspace_scan_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceScanResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "workspace_root",
        "file_count",
        "added",
        "updated",
        "removed",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace scan result does not match the IPC schema")

    counts = (
        result["schema_version"],
        result["file_count"],
        result["added"],
        result["updated"],
        result["removed"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise IpcProtocolError("daemon workspace scan counts have invalid field types")

    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    workspace_root_value = _bounded_response_string(
        result["workspace_root"],
        "workspace_root",
        _HINT_PATH_MAX_LENGTH,
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace scan root must be absolute")

    return WorkspaceScanResult(
        schema_version=result["schema_version"],
        workspace_id=workspace_id,
        project_id=project_id,
        workspace_root=workspace_root,
        file_count=result["file_count"],
        added=result["added"],
        updated=result["updated"],
        removed=result["removed"],
    )


def _success_result(response: dict[str, Any], *, expected_request_id: str) -> dict[str, Any]:
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
    if not isinstance(result, dict):
        raise IpcProtocolError("daemon result payload must be an object")
    return cast(dict[str, Any], result)


def _bounded_response_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise IpcProtocolError(f"daemon response has invalid {field}")
    return value


def _require_posix_transport() -> None:
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise UnsupportedIpcTransportError(
            "the Windows local-user IPC transport is not implemented in this bounded slice"
        )
