from __future__ import annotations

import os
import socket
import sqlite3
import stat
from pathlib import Path
from threading import Event

from harness.ipc import (
    IpcMessageTooLargeError,
    IpcProtocolError,
    StatusResult,
    UnsupportedIpcTransportError,
    receive_request,
    send_error_response,
    send_status_response,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database

_CLIENT_TIMEOUT_SECONDS = 2.0
_ACCEPT_POLL_SECONDS = 0.2


class DaemonError(RuntimeError):
    """Base class for bounded Harness daemon runtime failures."""


class InsecureSocketDirectoryError(DaemonError):
    """Raised when the IPC directory is not private to the current OS user."""


class SocketPathInUseError(DaemonError):
    """Raised when daemon startup would replace an existing filesystem entry."""


def read_daemon_status(connection: sqlite3.Connection) -> StatusResult:
    """Read one compact consistent status snapshot without mutating durable business state."""
    connection.execute("BEGIN")
    try:
        project_count = _table_count(connection, "projects")
        workspace_count = _table_count(connection, "workspaces")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return StatusResult(
        schema_version=SCHEMA_VERSION,
        project_count=project_count,
        workspace_count=workspace_count,
    )


def serve_daemon(
    database_path: Path,
    socket_path: Path,
    *,
    stop_event: Event | None = None,
) -> None:
    """Serve the first bounded read-only IPC path until asked to stop."""
    _require_posix_transport()
    _prepare_socket_parent(socket_path.parent)
    if socket_path.exists() or socket_path.is_symlink():
        raise SocketPathInUseError(f"refusing to replace existing IPC path: {socket_path}")

    initialize_database(database_path)
    database = connect_database(database_path)
    server: socket.socket | None = None
    socket_identity: tuple[int, int] | None = None
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        socket_stat = socket_path.lstat()
        socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
        server.listen()
        server.settimeout(_ACCEPT_POLL_SECONDS)
        while stop_event is None or not stop_event.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(_CLIENT_TIMEOUT_SECONDS)
                try:
                    _serve_client(client, database)
                except OSError:
                    continue
    finally:
        if server is not None:
            server.close()
        database.close()
        _unlink_owned_socket(socket_path, socket_identity)


def _serve_client(client: socket.socket, database: sqlite3.Connection) -> None:
    try:
        request = receive_request(client)
    except IpcMessageTooLargeError:
        _try_send_error(client, code="message_too_large", message="IPC request exceeds byte limit")
        return
    except (IpcProtocolError, TimeoutError):
        _try_send_error(client, code="invalid_request", message="IPC request is invalid")
        return

    try:
        status = read_daemon_status(database)
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request.request_id,
            code="database_error",
            message="daemon could not read status",
        )
        return
    send_status_response(client, request.request_id, status)


def _try_send_error(
    client: socket.socket,
    *,
    code: str,
    message: str,
    request_id: str | None = None,
) -> None:
    try:
        send_error_response(
            client,
            request_id=request_id,
            code=code,
            message=message,
        )
    except OSError:
        pass


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"projects", "workspaces"}:
        raise ValueError("unsupported status table")
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError(f"invalid count returned for {table}")
    return row[0]


def _prepare_socket_parent(parent: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise InsecureSocketDirectoryError(
            f"IPC directory must be owned by the current user with no group/other access: {parent}"
        )


def _unlink_owned_socket(socket_path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = socket_path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity and stat.S_ISSOCK(current.st_mode):
        socket_path.unlink()


def _require_posix_transport() -> None:
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise UnsupportedIpcTransportError(
            "the Windows local-user IPC transport is not implemented in this bounded slice"
        )
