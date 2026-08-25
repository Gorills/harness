from __future__ import annotations

import errno
import os
import socket
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from time import monotonic

from harness.git_workspace import (
    GitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_git_workspace_runtime_identity,
)
from harness.index import (
    IndexedFileRecord,
    IndexingError,
    ScanDeadlineExceededError,
    get_indexed_file,
    scan_workspace,
)
from harness.ipc import (
    IpcMessageTooLargeError,
    IpcProtocolError,
    StatusResult,
    TaskCheckpointRequestData,
    TaskCheckpointResult,
    TaskStartRequestData,
    TaskStartResult,
    UnsupportedIpcTransportError,
    WorkspaceIndexEntryResult,
    WorkspaceScanResult,
    WorkspaceSearchHit,
    WorkspaceSearchResult,
    WorkspaceStatusResult,
    WorkspaceTaskCheckpointSummary,
    WorkspaceTaskStatusResult,
    WorkspaceTaskSummary,
    receive_request,
    send_error_response,
    send_status_response,
    send_task_checkpoint_response,
    send_task_start_response,
    send_workspace_index_entry_response,
    send_workspace_scan_response,
    send_workspace_search_response,
    send_workspace_status_response,
    send_workspace_task_status_response,
)
from harness.knowledge import KnowledgeError, KnowledgeValidationError
from harness.registry import (
    RegistryError,
    WorkspaceRecord,
    get_project,
    get_workspace,
    list_workspaces,
    register_workspace_for_scan,
)
from harness.search import IndexedPathSearchScope, SearchError, search_indexed_paths
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.task_baseline import TaskBaselineError
from harness.task_checkpoints import (
    TaskCheckpointMechanicalError,
    get_latest_task_checkpoint_status,
)
from harness.task_workflow import (
    task_checkpoint as domain_task_checkpoint,
)
from harness.task_workflow import (
    task_resume as domain_task_resume,
)
from harness.task_workflow import (
    task_start as domain_task_start,
)
from harness.tasks import (
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskRevisionConflictError,
    TaskTransitionError,
    TaskValidationError,
    TaskWorkspaceConflictError,
    get_relevant_task,
)
from harness.watcher import (
    DEFAULT_WATCH_DEBOUNCE_SECONDS,
    DEFAULT_WATCH_FULL_RECONCILE_SECONDS,
    DEFAULT_WATCH_POLL_SECONDS,
    DEFAULT_WATCH_RETRY_SECONDS,
    DEFAULT_WATCH_SCAN_DEADLINE_SECONDS,
    DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS,
    run_workspace_watcher,
)
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


class WorkspaceIndexEntryNotFoundError(RuntimeError):
    """Raised when an exact current Structural Index entry does not exist."""


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


def read_workspace_task_status(
    connection: sqlite3.Connection,
    hints: Sequence[WorkspaceHint],
) -> WorkspaceTaskStatusResult:
    """Read the current working or most relevant waiting Task for one Workspace."""
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

    connection.execute("BEGIN")
    try:
        current_workspace = get_workspace(connection, workspace.workspace_id)
        if current_workspace != workspace:
            raise WorkspaceResolutionError("workspace registry identity changed during Task status")
        task = get_relevant_task(connection, workspace.workspace_id)
        checkpoint = (
            get_latest_task_checkpoint_status(connection, task.task_id)
            if task is not None
            else None
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    if inspect_git_workspace_runtime_identity(workspace.workspace_root) != runtime_identity:
        raise WorkspaceResolutionError("workspace Git identity changed during Task status")

    task_summary = (
        None
        if task is None
        else WorkspaceTaskSummary(
            task_id=task.task_id,
            title=task.title,
            state=task.state,
            wait_reason=task.wait_reason,
            revision=task.revision,
        )
    )
    checkpoint_summary = (
        None
        if checkpoint is None
        else WorkspaceTaskCheckpointSummary(
            checkpoint_id=checkpoint.checkpoint_id,
            task_revision=checkpoint.task_revision,
            state=checkpoint.state,
            wait_reason=checkpoint.wait_reason,
            next_step=checkpoint.next_step,
        )
    )
    return WorkspaceTaskStatusResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        task=task_summary,
        last_checkpoint=checkpoint_summary,
    )


def read_workspace_search(
    connection: sqlite3.Connection,
    hints: Sequence[WorkspaceHint],
    query: str,
    limit: int,
    scope: IndexedPathSearchScope = IndexedPathSearchScope.ALL,
) -> WorkspaceSearchResult:
    """Resolve one registered Workspace and search its current Structural Index snapshot."""
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

    connection.execute("BEGIN")
    try:
        current_workspace = get_workspace(connection, workspace.workspace_id)
        if current_workspace != workspace:
            raise WorkspaceResolutionError("workspace registry identity changed during search")
        project = get_project(connection, workspace.project_id)
        search_results = search_indexed_paths(
            connection,
            workspace.workspace_id,
            query,
            limit=limit,
            scope=scope,
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    if inspect_git_workspace_runtime_identity(workspace.workspace_root) != runtime_identity:
        raise WorkspaceResolutionError("workspace Git identity changed during search")

    return WorkspaceSearchResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        project_id=project.project_id,
        workspace_root=workspace.workspace_root,
        results=tuple(
            WorkspaceSearchHit(
                relative_path=result.relative_path,
                kind=result.kind,
                size_bytes=result.size_bytes,
                match_kind=result.match_kind,
            )
            for result in search_results
        ),
    )


def read_workspace_index_entry(
    connection: sqlite3.Connection,
    hints: Sequence[WorkspaceHint],
    relative_path: str,
) -> WorkspaceIndexEntryResult:
    """Resolve one Workspace and return one exact current Structural Index entry."""
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

    connection.execute("BEGIN")
    try:
        current_workspace = get_workspace(connection, workspace.workspace_id)
        if current_workspace != workspace:
            raise WorkspaceResolutionError(
                "workspace registry identity changed during index entry read"
            )
        project = get_project(connection, workspace.project_id)
        entry = get_indexed_file(connection, workspace.workspace_id, relative_path)
        if entry is None:
            raise WorkspaceIndexEntryNotFoundError
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    if inspect_git_workspace_runtime_identity(workspace.workspace_root) != runtime_identity:
        raise WorkspaceResolutionError("workspace Git identity changed during index entry read")

    return _workspace_index_entry_result(workspace, project.project_id, entry)


def _workspace_index_entry_result(
    workspace: WorkspaceRecord,
    project_id: str,
    entry: IndexedFileRecord,
) -> WorkspaceIndexEntryResult:
    return WorkspaceIndexEntryResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        project_id=project_id,
        workspace_root=workspace.workspace_root,
        relative_path=entry.relative_path,
        kind=entry.kind,
        size_bytes=entry.size_bytes,
    )


def mutate_task_start(
    connection: sqlite3.Connection,
    request: TaskStartRequestData,
) -> TaskStartResult:
    """Resolve one Workspace and delegate Task create/resume to the domain workflow."""
    workspace = _resolve_task_workspace(connection, request.workspace_hints)
    if request.task_id is None:
        if request.title is None:
            raise TaskValidationError("new task_start requires title")
        task = domain_task_start(
            connection,
            workspace.workspace_id,
            request.title,
            stack_hints=request.stack_hints,
        )
    else:
        task = domain_task_resume(
            connection,
            workspace.workspace_id,
            request.task_id,
            expected_revision=request.expected_revision,
        )
    return TaskStartResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        task_id=task.task_id,
        state=task.state,
        wait_reason=task.wait_reason,
        revision=task.revision,
    )


def mutate_task_checkpoint(
    connection: sqlite3.Connection,
    request: TaskCheckpointRequestData,
) -> TaskCheckpointResult:
    """Resolve one Workspace and delegate one explicit revision-CAS checkpoint."""
    workspace = _resolve_task_workspace(connection, request.workspace_hints)
    mutation = domain_task_checkpoint(
        connection,
        workspace.workspace_id,
        request.task_id,
        expected_revision=request.expected_revision,
        state=request.state,
        summary=request.summary,
        next_step=request.next_step,
        wait_reason=request.wait_reason,
        knowledge=request.knowledge,
    )
    return TaskCheckpointResult(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace.workspace_id,
        task_id=mutation.task.task_id,
        state=mutation.task.state,
        wait_reason=mutation.task.wait_reason,
        revision=mutation.task.revision,
        checkpoint_id=mutation.checkpoint.checkpoint_id,
        knowledge_ids=tuple(card.knowledge_id for card in mutation.knowledge_cards),
    )


def _resolve_task_workspace(
    connection: sqlite3.Connection,
    hints: Sequence[WorkspaceHint],
) -> WorkspaceRecord:
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
    return workspace


def scan_workspace_path(
    connection: sqlite3.Connection,
    path: Path,
    *,
    deadline: float | None = None,
) -> WorkspaceScanResult:
    """Register/reuse one Git Workspace and run a bounded deterministic reconciliation."""
    effective_deadline = monotonic() + _SCAN_DEADLINE_SECONDS if deadline is None else deadline
    registration = register_workspace_for_scan(connection, path=path)
    scan = scan_workspace(
        connection,
        registration.workspace.workspace_id,
        deadline=effective_deadline,
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
    watcher_poll_seconds: float = DEFAULT_WATCH_POLL_SECONDS,
    watcher_debounce_seconds: float = DEFAULT_WATCH_DEBOUNCE_SECONDS,
    watcher_full_reconcile_seconds: float = DEFAULT_WATCH_FULL_RECONCILE_SECONDS,
    watcher_retry_seconds: float = DEFAULT_WATCH_RETRY_SECONDS,
    watcher_token_deadline_seconds: float = DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS,
    watcher_scan_deadline_seconds: float = DEFAULT_WATCH_SCAN_DEADLINE_SECONDS,
) -> None:
    """Serve local IPC while keeping registered Workspace indexes reconciled."""
    _require_posix_transport()
    _prepare_socket_parent(socket_path.parent)
    socket_lock_fd = _acquire_daemon_lock(socket_path)

    database_lock_fd: int | None = None
    database: sqlite3.Connection | None = None
    server: socket.socket | None = None
    socket_identity: tuple[int, int] | None = None
    scan_lock = Lock()
    watcher_stop = Event()
    watcher_thread: Thread | None = None
    watcher_failures: SimpleQueue[Exception] = SimpleQueue()
    watcher_invalidations: SimpleQueue[str] = SimpleQueue()
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

        def watcher_target() -> None:
            try:
                run_workspace_watcher(
                    database_path,
                    watcher_stop,
                    scan_lock,
                    poll_seconds=watcher_poll_seconds,
                    debounce_seconds=watcher_debounce_seconds,
                    full_reconcile_seconds=watcher_full_reconcile_seconds,
                    retry_seconds=watcher_retry_seconds,
                    token_deadline_seconds=watcher_token_deadline_seconds,
                    scan_deadline_seconds=watcher_scan_deadline_seconds,
                    invalidations=watcher_invalidations,
                )
            except Exception as exc:
                watcher_failures.put(exc)

        thread = Thread(
            target=watcher_target,
            name="harness-workspace-watcher",
            daemon=True,
        )
        thread.start()
        watcher_thread = thread
        while stop_event is None or not stop_event.is_set():
            if not watcher_thread.is_alive():
                try:
                    watcher_failure = watcher_failures.get_nowait()
                except Empty:
                    watcher_failure = None
                if watcher_failure is not None:
                    raise DaemonError("Workspace watcher stopped unexpectedly") from watcher_failure
                raise DaemonError("Workspace watcher stopped unexpectedly")
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(_CLIENT_TIMEOUT_SECONDS)
                try:
                    _serve_client(client, database, scan_lock, watcher_invalidations)
                except OSError:
                    continue
    finally:
        watcher_stop.set()
        if watcher_thread is not None:
            watcher_thread.join()
        if server is not None:
            server.close()
        if database is not None:
            database.close()
        _unlink_owned_socket(socket_path, socket_identity)
        if database_lock_fd is not None:
            os.close(database_lock_fd)
        os.close(socket_lock_fd)


def _serve_client(
    client: socket.socket,
    database: sqlite3.Connection,
    scan_lock: Lock,
    watcher_invalidations: SimpleQueue[str],
) -> None:
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
    if request.method == "workspace_task_status":
        _serve_workspace_task_status(client, database, request.request_id, request.workspace_hints)
        return
    if (
        request.method == "workspace_search"
        and request.search_query is not None
        and request.search_limit is not None
        and request.search_scope is not None
    ):
        _serve_workspace_search(
            client,
            database,
            request.request_id,
            request.workspace_hints,
            request.search_query,
            request.search_limit,
            request.search_scope,
        )
        return
    if request.method == "workspace_index_entry" and request.index_relative_path is not None:
        _serve_workspace_index_entry(
            client,
            database,
            request.request_id,
            request.workspace_hints,
            request.index_relative_path,
        )
        return
    if request.method == "task_start" and request.task_start is not None:
        _serve_task_start(client, database, request.request_id, request.task_start)
        return
    if request.method == "task_checkpoint" and request.task_checkpoint is not None:
        _serve_task_checkpoint(client, database, request.request_id, request.task_checkpoint)
        return
    if request.method == "scan_workspace" and request.scan_path is not None:
        _serve_workspace_scan(
            client,
            database,
            request.request_id,
            request.scan_path,
            scan_lock,
            watcher_invalidations,
        )
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


def _serve_workspace_task_status(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    hints: Sequence[WorkspaceHint],
) -> None:
    try:
        status = read_workspace_task_status(database, hints)
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
    except TaskError:
        _try_send_error(
            client,
            request_id=request_id,
            code="task_error",
            message="daemon could not read Task status",
        )
        return
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request_id,
            code="database_error",
            message="daemon could not read Task status",
        )
        return
    try:
        send_workspace_task_status_response(client, request_id, status)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Workspace Task status exceeds IPC byte limit",
        )


def _serve_workspace_search(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    hints: Sequence[WorkspaceHint],
    query: str,
    limit: int,
    scope: IndexedPathSearchScope,
) -> None:
    try:
        result = read_workspace_search(database, hints, query, limit, scope)
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
    except IndexingError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="index_error",
            message=str(exc),
        )
        return
    except SearchError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="search_error",
            message=str(exc),
        )
        return
    except sqlite3.DatabaseError:
        _try_send_error(
            client,
            request_id=request_id,
            code="database_error",
            message="daemon could not search Workspace index",
        )
        return

    try:
        send_workspace_search_response(client, request_id, result)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Workspace search result exceeds IPC byte limit",
        )


def _serve_workspace_index_entry(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    hints: Sequence[WorkspaceHint],
    relative_path: str,
) -> None:
    try:
        result = read_workspace_index_entry(database, hints, relative_path)
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
    except WorkspaceIndexEntryNotFoundError:
        _try_send_error(
            client,
            request_id=request_id,
            code="index_entry_not_found",
            message="requested Structural Index entry is not present",
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
            message="daemon could not read Workspace index entry",
        )
        return
    try:
        send_workspace_index_entry_response(client, request_id, result)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Workspace index entry exceeds IPC byte limit",
        )


def _serve_task_start(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    request: TaskStartRequestData,
) -> None:
    try:
        result = mutate_task_start(database, request)
    except WorkspaceResolutionError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="workspace_resolution_error",
            message=str(exc),
        )
        return
    except TaskRevisionConflictError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_revision_conflict", message=str(exc)
        )
        return
    except TaskWorkspaceConflictError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_workspace_conflict", message=str(exc)
        )
        return
    except TaskNotFoundError as exc:
        _try_send_error(client, request_id=request_id, code="task_not_found", message=str(exc))
        return
    except TaskConflictError as exc:
        _try_send_error(client, request_id=request_id, code="task_conflict", message=str(exc))
        return
    except TaskTransitionError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_transition_error", message=str(exc)
        )
        return
    except TaskValidationError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_validation_error", message=str(exc)
        )
        return
    except TaskBaselineError:
        _try_send_error(
            client,
            request_id=request_id,
            code="task_mechanical_error",
            message="daemon could not capture Task mechanical baseline",
        )
        return
    except GitWorkspaceError as exc:
        _try_send_error(client, request_id=request_id, code="workspace_git_error", message=str(exc))
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
            message="daemon could not mutate Task state",
        )
        return
    try:
        send_task_start_response(client, request_id, result)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Task result exceeds IPC byte limit",
        )


def _serve_task_checkpoint(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    request: TaskCheckpointRequestData,
) -> None:
    try:
        result = mutate_task_checkpoint(database, request)
    except WorkspaceResolutionError as exc:
        _try_send_error(
            client,
            request_id=request_id,
            code="workspace_resolution_error",
            message=str(exc),
        )
        return
    except TaskRevisionConflictError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_revision_conflict", message=str(exc)
        )
        return
    except TaskWorkspaceConflictError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_workspace_conflict", message=str(exc)
        )
        return
    except TaskNotFoundError as exc:
        _try_send_error(client, request_id=request_id, code="task_not_found", message=str(exc))
        return
    except TaskConflictError as exc:
        _try_send_error(client, request_id=request_id, code="task_conflict", message=str(exc))
        return
    except TaskTransitionError as exc:
        _try_send_error(
            client, request_id=request_id, code="task_transition_error", message=str(exc)
        )
        return
    except (TaskValidationError, KnowledgeValidationError) as exc:
        _try_send_error(
            client, request_id=request_id, code="task_validation_error", message=str(exc)
        )
        return
    except (TaskCheckpointMechanicalError, KnowledgeError):
        _try_send_error(
            client,
            request_id=request_id,
            code="task_mechanical_error",
            message="daemon could not capture Task checkpoint evidence",
        )
        return
    except GitWorkspaceError as exc:
        _try_send_error(client, request_id=request_id, code="workspace_git_error", message=str(exc))
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
            message="daemon could not checkpoint Task",
        )
        return
    try:
        send_task_checkpoint_response(client, request_id, result)
    except IpcMessageTooLargeError:
        _try_send_error(
            client,
            request_id=request_id,
            code="response_too_large",
            message="Task checkpoint result exceeds IPC byte limit",
        )


def _serve_workspace_scan(
    client: socket.socket,
    database: sqlite3.Connection,
    request_id: str,
    path: Path,
    scan_lock: Lock,
    watcher_invalidations: SimpleQueue[str],
) -> None:
    deadline = monotonic() + _SCAN_DEADLINE_SECONDS
    try:
        remaining = deadline - monotonic()
        if remaining <= 0 or not scan_lock.acquire(timeout=remaining):
            raise ScanDeadlineExceededError("Workspace scan deadline exceeded")
        try:
            result = scan_workspace_path(database, path, deadline=deadline)
        finally:
            scan_lock.release()
        watcher_invalidations.put(result.workspace_id)
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
