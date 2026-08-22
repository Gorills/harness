from __future__ import annotations

import errno
import os
import socket
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path
from threading import Event
from time import monotonic

from harness.git_workspace import (
    GitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_git_workspace_runtime_identity,
)
from harness.index import IndexingError, ScanDeadlineExceededError, scan_workspace
from harness.ipc import (
    IpcMessageTooLargeError,
    IpcProtocolError,
    StatusResult,
    UnsupportedIpcTransportError,
    WorkspaceScanResult,
    WorkspaceStatusResult,
    receive_request,
    send_error_response,
    send_status_response,
    send_workspace_scan_response,
    send_workspace_status_response,
)
from harness.registry import (
    RegistryError,
    get_project,
    get_workspace,
    list_workspaces,
    register_workspace_for_scan,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.workspace_resolution import (
    WorkspaceCandidate,
    WorkspaceHint,
    WorkspaceResolutionError,
    WorkspaceResolver,
)

_CLIENT_TIMEOUT_SECONDS = 2.0
_ACCEPT_POLL_SECONDS = 0.2
_ERROR_MESSAGE_MAX_LENGTH = 1024
_SCAN_DEADLINE_SECONDS = 30.0
_EXISTING_SOCKET_PROBE_SECONDS = 0.2


class DaemonError(RuntimeError):
    """Base class for bounded Harness daemon runtime failures."""


class InsecureSocketDirectoryError(DaemonError):
    """Raised when the IPC directory is not private to the current OS user."""


class InsecureDaemonLockError(DaemonError):
    """Raised when a daemon singleton lock path is not safe to use."""


class DaemonAlreadyRunningError(DaemonError):
    """Raised when another daemon already owns the selected endpoint or database."""


class SocketPathInUseError(DaemonError):
    """Raised when daemon startup would replace an unsafe existing filesystem entry."""


def read_daemon_status(connection: sqlite3.Connection) -> StatusResult:
    """Read one compact consistent global status snapshot without durable mutation."""
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


def read_workspace_status(
    connection: sqlite3.Connection,
    hints: Sequence[WorkspaceHint],
) -> WorkspaceStatusResult:
    """Resolve one registered Workspace and read its bounded live/derived status."""
    registered = list_workspaces(connection)
    resolution = WorkspaceResolver(
        [
            WorkspaceCandidate(workspace_id=workspace.workspace_id, root=workspace.workspace_root)
            for workspace in registered
        ]
    ).resolve(hints)
    workspace = get_workspace(connection, resolution.workspace_id)

    runtime_identity = inspect_git_workspace_runtime_identity(workspace.workspace_root)
    if (
        runtime_identity.layout.workspace_root != workspace.workspace_root
        or runtime_identity.layout.git_common_dir != workspace.git_common_dir
    ):
        raise WorkspaceResolutionError(
            f"registered workspace Git identity changed: {workspace.workspace_root}"
        )
    git_status = inspect_git_working_tree_status(workspace.workspace_root)
    if inspect_git_workspace_runtime_identity(workspace.workspace_root) != runtime_identity:
        raise WorkspaceResolutionError("workspace Git identity changed during status read")

    connection.execute("BEGIN")
    try:
        current_workspace = get_workspace(connection, workspace.workspace_id)
        if current_workspace != workspace:
            raise WorkspaceResolutionError("workspace registry identity changed during status read")
        project = get_project(connection, workspace.project_id)
        indexed_file_count = _indexed_file_count(connection, workspace.workspace_id)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    return WorkspaceStatusResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        project_id=workspace.project_id,
        visibility_mode=project.visibility_mode.value,
        workspace_root=workspace.workspace_root,
        head=git_status.head,
        branch=git_status.branch,
        dirty_path_count=git_status.dirty_path_count,
        indexed_file_count=indexed_file_count,
    )


def scan_workspace_path(connection: sqlite3.Connection, path: Path) -> WorkspaceScanResult:
    """Register/reuse one Git Workspace and run a bounded deterministic reconciliation."""
    deadline = monotonic() + _SCAN_DEADLINE_SECONDS
    registration = register_workspace_for_scan(connection, path=path)
    scan = scan_workspace(
        connection,
        registration.workspace.workspace_id,
        deadline=deadline,
    )
    return WorkspaceScanResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=registration.workspace.workspace_id,
        project_id=registration.project.project_id,
        visibility_mode=registration.project.visibility_mode.value,
        workspace_root=registration.workspace.workspace_root,
        project_created=registration.project_created,
        workspace_created=registration.workspace_created,
        file_count=scan.file_count,
        added=scan.added,
        updated=scan.updated,
        removed=scan.removed,
    )


def serve_daemon(
    database_path: Path,
    socket_path: Path,
    *,
    stop_event: Event | None = None,
) -> None:
    """Serve bounded local IPC status and deterministic scan paths until asked to stop."""
    _require_posix_transport()
    _prepare_socket_parent(socket_path.parent)
    socket_lock_fd = _acquire_daemon_lock(socket_path)

    database_lock_fd: int | None = None
    database: sqlite3.Connection | None = None
    server: socket.socket | None = None
    socket_identity: tuple[int, int] | None = None
    try:
        _prepare_socket_path_for_bind(socket_path)
        database_lock_fd = _acquire_database_lock(database_path)
        initialize_database(database_path)
        database = connect_database(database_path)

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
        if database is not None:
            database.close()
        _unlink_owned_socket(socket_path, socket_identity)
        if database_lock_fd is not None:
            os.close(database_lock_fd)
        os.close(socket_lock_fd)


def _serve_client(client: socket.socket, database: sqlite3.Connection) -> None:
    try:
        request = receive_request(client)
    except IpcMessageTooLargeError:
        _try_send_error(client, code="message_too_large", message="IPC request exceeds byte limit")
        return
    except (IpcProtocolError, TimeoutError):
        _try_send_error(client, code="invalid_request", message="IPC request is invalid")
        return

    if request.method == "status":
        _serve_global_status(client, database, request.request_id)
        return
    if request.method == "workspace_status":
        _serve_workspace_status(client, database, request.request_id, request.workspace_hints)
        return
    if request.method == "scan_workspace" and request.scan_path is not None:
        _serve_workspace_scan(client, database, request.request_id, request.scan_path)
        return
    _try_send_error(
        client,
        request_id=request.request_id,
        code="invalid_request",
        message="IPC request is invalid",
    )


def _serve_global_status(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
) -> None:
    try:
        status = read_daemon_status(database)
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request_id,
            code="database_error",
            message="daemon could not read status",
        )
        return
    send_status_response(client, request_id, status)


def _serve_workspace_status(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    hints: Sequence[WorkspaceHint],
) -> None:
    try:
        status = read_workspace_status(database, hints)
    except WorkspaceResolutionError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="workspace_resolution_error",
            message=str(exc),
        )
        return
    except GitWorkspaceError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="workspace_git_error",
            message=str(exc),
        )
        return
    except RegistryError:
        _try_send_error(
            client,
            request_id=request_id,
            code="registry_error",
            message="daemon could not read Workspace registry state",
        )
        return
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request_id,
            code="database_error",
            message="daemon could not read Workspace status",
        )
        return
    try:
        send_workspace_status_response(client, request_id, status)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Workspace status exceeds IPC byte limit",
        )


def _serve_workspace_scan(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    path: Path,
) -> None:
    try:
        result = scan_workspace_path(database, path)
    except ScanDeadlineExceededError:
        _try_send_error(
            client,
            request_id=request_id,
            code="scan_timeout",
            message="Workspace scan exceeded the daemon execution deadline",
        )
        return
    except GitWorkspaceError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="workspace_git_error",
            message=str(exc),
        )
        return
    except RegistryError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="registry_error",
            message=str(exc),
        )
        return
    except IndexingError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="index_error",
            message=str(exc),
        )
        return
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request_id,
            code="database_error",
            message="daemon could not register or scan Workspace",
        )
        return

    try:
        send_workspace_scan_response(client, request_id, result)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Workspace scan result exceeds IPC byte limit",
        )


def _try_send_error(
    client: socket.socket,
    *,
    code: str,
    message: str,
    request_id: str | None = None,
) -> None:
    if len(message) > _ERROR_MESSAGE_MAX_LENGTH:
        message = f"{message[: _ERROR_MESSAGE_MAX_LENGTH - 3]}..."
    try:
        send_error_response(
            client,
            request_id=request_id,
            code=code,
            message=message,
        )
    except (IpcMessageTooLargeError, OSError):
        pass


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"projects", "workspaces"}:
        raise ValueError("unsupported status table")
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError(f"invalid count returned for {table}")
    return row[0]


def _indexed_file_count(connection: sqlite3.Connection, workspace_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM indexed_files WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError("invalid indexed file count")
    return row[0]


def _prepare_socket_parent(parent: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise InsecureSocketDirectoryError(
            f"IPC directory must be owned by the current user, be a real directory, "
            f"and have no group/other access: {parent}"
        )


def _daemon_lock_path(socket_path: Path) -> Path:
    return socket_path.with_name(f"{socket_path.name}.lock")


def _database_lock_path(database_path: Path) -> Path:
    try:
        resolved = database_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InsecureDaemonLockError(
            f"daemon database path could not be resolved: {database_path}"
        ) from exc
    return resolved.with_name(f"{resolved.name}.lock")


def _acquire_daemon_lock(socket_path: Path) -> int:
    return _acquire_lock_file(
        _daemon_lock_path(socket_path),
        conflict_message=f"another Harness daemon already owns the IPC endpoint: {socket_path}",
    )


def _acquire_database_lock(database_path: Path) -> int:
    lock_path = _database_lock_path(database_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InsecureDaemonLockError(
            f"daemon database lock parent could not be prepared: {lock_path.parent}"
        ) from exc
    return _acquire_lock_file(
        lock_path,
        conflict_message=f"another Harness daemon already owns the database: {database_path}",
    )


def _acquire_lock_file(lock_path: Path, *, conflict_message: str) -> int:
    import fcntl

    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise InsecureDaemonLockError(
            f"daemon lock path could not be inspected: {lock_path}"
        ) from exc

    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise InsecureDaemonLockError(f"daemon lock path must be a regular file: {lock_path}")

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise InsecureDaemonLockError(
            f"daemon lock path could not be opened safely: {lock_path}"
        ) from exc

    try:
        opened = os.fstat(lock_fd)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise InsecureDaemonLockError(
                f"daemon lock must be a current-user regular file with one link: {lock_path}"
            )
        os.fchmod(lock_fd, 0o600)
        if stat.S_IMODE(os.fstat(lock_fd).st_mode) != 0o600:
            raise InsecureDaemonLockError(f"daemon lock mode could not be secured: {lock_path}")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DaemonAlreadyRunningError(conflict_message) from exc
        except OSError as exc:
            raise DaemonError(f"daemon singleton lock could not be acquired: {lock_path}") from exc
    except Exception:
        os.close(lock_fd)
        raise
    return lock_fd


def _prepare_socket_path_for_bind(socket_path: Path) -> None:
    try:
        current = socket_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SocketPathInUseError(f"refusing to replace existing IPC path: {socket_path}") from exc

    if not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.geteuid():
        raise SocketPathInUseError(f"refusing to replace existing IPC path: {socket_path}")

    identity = (current.st_dev, current.st_ino)
    if _socket_endpoint_accepts_connections(socket_path):
        raise DaemonAlreadyRunningError(
            f"another process is already serving the IPC endpoint: {socket_path}"
        )

    try:
        replacement = socket_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SocketPathInUseError(f"refusing to replace existing IPC path: {socket_path}") from exc

    if (
        not stat.S_ISSOCK(replacement.st_mode)
        or replacement.st_uid != os.geteuid()
        or (replacement.st_dev, replacement.st_ino) != identity
    ):
        raise SocketPathInUseError(f"refusing to replace changed IPC path: {socket_path}")
    socket_path.unlink()


def _socket_endpoint_accepts_connections(socket_path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(_EXISTING_SOCKET_PROBE_SECONDS)
        try:
            probe.connect(str(socket_path))
        except TimeoutError:
            return True
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                return False
            raise SocketPathInUseError(
                f"refusing to replace IPC socket that could not be classified: {socket_path}"
            ) from exc
    return True


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
